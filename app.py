"""
BuildBot — Flask application.

Multi-job, three-phase chat flow:

  Phase 1   POST /chat  {"type": "message",    "message": "..."}
  Phase 1b  POST /chat  {"type": "job_select", "job_name": "..."}
  Phase 2   POST /chat  {"type": "param_submit", "params": {...}}
"""

import json
import logging
import logging.handlers
import os
import re
import threading
import time
import uuid
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from urllib.parse import urlparse, urljoin

load_dotenv()

# ─── Directories ──────────────────────────────────────────────────────────────────
_LOG_DIR      = Path(os.getenv("LOG_DIR",      "logs"))
_SESSION_DIR  = Path(os.getenv("SESSION_DIR",  "flask_sessions"))
_LOG_DIR.mkdir(exist_ok=True)
_SESSION_DIR.mkdir(exist_ok=True)

# Persistence files — in-memory state is written here so it survives restarts
_CHATS_FILE = _SESSION_DIR / "user_chats.json"
_CONV_FILE  = _SESSION_DIR / "conv_sessions.json"

# ─── Audit log (persistent, rotating 10 MB × 10 files) ──────────────────────────
_audit_handler = logging.handlers.RotatingFileHandler(
    _LOG_DIR / "audit.log",
    maxBytes=10 * 1024 * 1024,
    backupCount=10,
    encoding="utf-8",
)
_audit_handler.setFormatter(
    logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s")
)
audit_logger = logging.getLogger("buildbot.audit")
audit_logger.addHandler(_audit_handler)
audit_logger.setLevel(logging.INFO)
audit_logger.propagate = False   # security events go to audit.log only

# ─── App logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def _sl(value, maxlen: int = 200) -> str:
    """
    Sanitize a user-supplied value for safe logging (CWE-117 / CWE-93).

    Replaces newlines, carriage returns, and tabs with underscores so an
    attacker cannot inject fake log entries by embedding control characters
    in a username, job name, IP address, or error message.
    Also truncates to `maxlen` characters to prevent log flooding.
    """
    return re.sub(r"[\r\n\t]", "_", str(value))[:maxlen]

# ─── Flask app ────────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "")
if not app.secret_key:
    raise RuntimeError(
        "FLASK_SECRET_KEY is not set. "
        "Generate one: python -c \"import secrets; print(secrets.token_hex(32))\""
    )

# ─── Daily secret-key rotation ────────────────────────────────────────────────────
# If MASTER_SECRET is set in .env, derive today's session key from it using HMAC-SHA256.
# A background thread swaps the key at midnight UTC every day — all sessions from the
# previous day are automatically invalidated (users log in fresh).
# MASTER_SECRET itself never changes; only the derived key rotates.

import hmac as _hmac, hashlib as _hashlib
from datetime import datetime as _datetime, timezone as _tz, timedelta as _td


def _derive_daily_key(master: str) -> str:
    """HMAC-SHA256(master, YYYY-MM-DD-UTC) → 64-char hex string."""
    date_str = _datetime.now(_tz.utc).strftime("%Y-%m-%d")
    return _hmac.new(master.encode("utf-8"), date_str.encode("utf-8"), _hashlib.sha256).hexdigest()


_master = os.getenv("MASTER_SECRET", "").strip()
if _master:
    app.secret_key = _derive_daily_key(_master)
    logger.info("Session key derived from MASTER_SECRET (daily rotation active).")

    def _midnight_rotation():
        """Sleep until next UTC midnight then rotate the session key."""
        while True:
            now     = _datetime.now(_tz.utc)
            next_mn = (now + _td(days=1)).replace(hour=0, minute=0, second=5, microsecond=0)
            time.sleep((next_mn - now).total_seconds())
            app.secret_key = _derive_daily_key(_master)
            audit_logger.info("SESSION_KEY_ROTATED  date=%s", next_mn.strftime("%Y-%m-%d"))
            logger.info("Session key rotated (daily rotation).")

    threading.Thread(target=_midnight_rotation, daemon=True, name="key-rotation").start()


# Return JSON for all unhandled errors so the frontend never sees "Session expired"
# when the real cause is a server exception or rate limit.
@app.errorhandler(500)
def _err500(e):
    logger.exception("Unhandled 500: %s", e)
    return jsonify({"reply": "❌ Server error. Check the app logs for details.", "param_card": None, "job_card": None}), 500


@app.errorhandler(401)
@app.errorhandler(403)
def _err_auth(e):
    return jsonify({"reply": "⚠️ Not authorised.", "param_card": None, "job_card": None}), e.code

# ─── Session configuration ────────────────────────────────────────────────────────
# Using Flask's built-in signed-cookie sessions.
# Flask-Session filesystem backend was causing session loss on API requests.
# The cookies are HMAC-signed with FLASK_SECRET_KEY — safe for our internal tool.
app.permanent_session_lifetime = 28800   # 8 hours absolute
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Strict"
_cookie_secure = os.getenv("COOKIE_SECURE", "0") == "1"
app.config["SESSION_COOKIE_SECURE"]   = _cookie_secure
if not _cookie_secure:
    logger.warning(
        "SESSION_COOKIE_SECURE is OFF (COOKIE_SECURE != '1'). "
        "Set COOKIE_SECURE=1 in .env when running over HTTPS to prevent "
        "the session cookie being sent over plain HTTP (CWE-614)."
    )
logger.info("Using Flask built-in cookie sessions (secure=%s, samesite=Strict)", _cookie_secure)

# ─── CSRF protection (Flask-WTF) — optional ──────────────────────────────────────
try:
    from flask_wtf.csrf import CSRFProtect as _CSRFProtect, generate_csrf as _generate_csrf
    # CSRF is ON by default for all routes.
    # JSON API routes are explicitly exempted below after the routes are defined.
    app.config['WTF_CSRF_CHECK_DEFAULT'] = True
    csrf = _CSRFProtect(app)
    _CSRF_AVAILABLE = True
    logger.info("Flask-WTF CSRF initialized")
except ImportError:
    _CSRF_AVAILABLE = False
    _generate_csrf = lambda: ""   # noqa: E731
    csrf = None
    logger.warning("flask-wtf not installed — CSRF protection disabled")

# ─── Rate limiting (Flask-Limiter) — optional ────────────────────────────────────
_LOGIN_LIMIT = os.getenv("RATE_LOGIN", "10 per minute")
_CHAT_LIMIT  = os.getenv("RATE_CHAT",  "30 per minute")

try:
    from flask_limiter import Limiter as _Limiter
    from flask_limiter.util import get_remote_address as _get_remote_address
    limiter = _Limiter(
        app=app,
        key_func=_get_remote_address,
        default_limits=[],
        storage_uri="memory://",
    )
    _LIMITER_AVAILABLE = True
    logger.info("Flask-Limiter initialized")

    # Return JSON on rate-limit so the frontend shows a proper message
    # instead of treating the 429 HTML as "Session expired"
    from flask_limiter.errors import RateLimitExceeded as _RateLimitExceeded
    @app.errorhandler(_RateLimitExceeded)
    def _handle_rate_limit(e):
        from flask import jsonify as _jsonify
        return _jsonify({
            "reply":      f"⏱️ Slow down — {e.description}",
            "param_card": None,
            "job_card":   None,
        }), 429
except ImportError:
    _LIMITER_AVAILABLE = False
    logger.warning("flask-limiter not installed — rate limiting disabled")

    # Dummy limiter decorator so routes compile cleanly
    class _DummyLimiter:
        def limit(self, *a, **kw):
            def decorator(fn): return fn
            return decorator
    limiter = _DummyLimiter()

# ─── Security headers (every response) ────────────────────────────────────────────
@app.after_request
def _security_headers(response):
    response.headers.setdefault("X-Frame-Options",        "DENY")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy",        "strict-origin-when-cross-origin")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self';"
    )
    response.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
    # Expose CSRF token to JavaScript via cookie
    if _CSRF_AVAILABLE and request.endpoint and request.endpoint not in ("static",):
        try:
            response.set_cookie(
                "csrf_token", _generate_csrf(),
                samesite="Lax", httponly=False,   # must be JS-readable for X-CSRFToken header
                secure=_cookie_secure,
            )
        except RuntimeError as _csrf_err:
            logger.debug("CSRF cookie skipped outside request context: %s", _csrf_err)
    return response

# Import auth module — keeps all auth logic in one place
from services.auth import (  # noqa: E402
    auth_mode, can_trigger_job, current_user, get_user_jenkins_credential,
    login_user, logout_user, require_admin, require_auth,
)


# ─── In-memory stores ────────────────────────────────────────────────────────────

# Build state: { build_job_id: {"status", "message", "done", "owner", "created_at", ...} }
_build_jobs: dict[str, dict] = {}

# Conversation sessions: { sess_key: {"job_name", "schema", ...} }
_conv_sessions: dict[str, dict] = {}

# User chat sessions: { flask_sid: { chat_id: {"title", "created_at"} } }
_user_chats: dict[str, dict] = {}


# ─── Persistence helpers (survive restarts) ───────────────────────────────────────
# _user_chats and _conv_sessions are written to flask_sessions/ on every mutation
# and loaded back at startup so a server restart is transparent to users.

def _save_user_chats() -> None:
    """Atomically persist _user_chats to disk."""
    try:
        tmp = _CHATS_FILE.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(_user_chats, fh)
        tmp.replace(_CHATS_FILE)
    except Exception as exc:
        logger.warning("Could not save user_chats: %s", exc)


def _load_user_chats() -> None:
    """Restore _user_chats from disk on startup."""
    if not _CHATS_FILE.exists():
        return
    try:
        with open(_CHATS_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            _user_chats.update(data)
            logger.info("Loaded %d user-chat entries from disk.", len(_user_chats))
    except Exception as exc:
        logger.warning("Could not load user_chats: %s", exc)


def _save_conv_sessions() -> None:
    """Atomically persist _conv_sessions to disk (skips non-serialisable entries)."""
    try:
        serialisable = {}
        for key, val in _conv_sessions.items():
            try:
                json.dumps(val)
                serialisable[key] = val
            except (TypeError, ValueError):
                pass
        tmp = _CONV_FILE.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(serialisable, fh)
        tmp.replace(_CONV_FILE)
    except Exception as exc:
        logger.warning("Could not save conv_sessions: %s", exc)


def _load_conv_sessions() -> None:
    """Restore _conv_sessions from disk on startup."""
    if not _CONV_FILE.exists():
        return
    try:
        with open(_CONV_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            _conv_sessions.update(data)
            logger.info("Loaded %d conv-session entries from disk.", len(_conv_sessions))
    except Exception as exc:
        logger.warning("Could not load conv_sessions: %s", exc)


# Load persisted state — must run after the dicts are declared above
_load_user_chats()
_load_conv_sessions()


# ─── TTL eviction (background thread) ────────────────────────────────────────────
_ENTRY_TTL_S = int(os.getenv("ENTRY_TTL_HOURS", "24")) * 3600


def _evict_stale_entries() -> None:
    """
    Background thread — runs every hour, removes entries older than ENTRY_TTL_HOURS.
    Prevents unbounded memory growth over long-running deployments.
    """
    while True:
        time.sleep(3600)
        cutoff = time.time() - _ENTRY_TTL_S
        removed = 0

        for job_id in list(_build_jobs.keys()):
            e = _build_jobs.get(job_id, {})
            if e.get("done") and e.get("created_at", 0) < cutoff:
                del _build_jobs[job_id]
                removed += 1

        for key in list(_conv_sessions.keys()):
            e = _conv_sessions.get(key, {})
            if e.get("created_at", 0) < cutoff:
                del _conv_sessions[key]
                removed += 1

        # Evict _user_chats entries for sessions inactive longer than TTL
        for sid in list(_user_chats.keys()):
            e = _user_chats.get(sid, {})
            if e.get("_created_at", 0) < cutoff:
                del _user_chats[sid]
                removed += 1

        if removed:
            logger.info("TTL eviction removed %d stale entries", removed)
            _save_user_chats()
            _save_conv_sessions()


threading.Thread(target=_evict_stale_entries, daemon=True, name="ttl-eviction").start()


# ─── Bootstrap admin account from .env ───────────────────────────────────────────
# Runs once at startup: if users.json has no users AND BOOTSTRAP_ADMIN_PASSWORD is set,
# create the admin account automatically so the app is usable without running the CLI.

def _bootstrap_admin() -> None:
    """Create the bootstrap admin from env vars if users.json is empty."""
    username = os.getenv("BOOTSTRAP_ADMIN_USER",     "admin").strip()
    display  = os.getenv("BOOTSTRAP_ADMIN_DISPLAY",  "Admin").strip()
    password = os.getenv("BOOTSTRAP_ADMIN_PASSWORD", "").strip()

    if not password:
        return   # nothing to do — operator chose manual setup

    try:
        from services.auth import (
            load_users, save_users, hash_password, validate_password,
        )
        db    = load_users()
        users = db.get("users", {})

        if users:
            return   # already populated — never overwrite

        errors = validate_password(password)
        if errors:
            logger.warning(
                "BOOTSTRAP_ADMIN_PASSWORD does not meet policy (%s) — skipping auto-create.",
                errors,
            )
            return

        db.setdefault("users", {})[username] = {
            "display_name":  display or username,
            "password_hash": hash_password(password),
            "role":          "admin",
            "allowed_jobs":  "*",
            "active":        True,
        }
        save_users(db)
        logger.info("Bootstrap admin '%s' created from env vars.", username)
        audit_logger.info("BOOTSTRAP_ADMIN_CREATED  user=%s", _sl(username))

    except Exception as exc:
        logger.warning("Bootstrap admin creation failed: %s", exc)


_bootstrap_admin()

# ─── Search index — background job-configuration indexer ─────────────────────────
# Discovers all Jenkins jobs recursively (including nested folders), fetches
# config.xml for each job, and builds a full-text + structured search index.
# Refreshes every SEARCH_INDEX_TTL seconds (default 600 = 10 min, set in .env).
# Runs as a daemon thread — if Jenkins is unreachable at startup the indexer
# silently skips and retries on the next scheduled cycle.
try:
    from services.jenkins_search import get_index as _get_search_index
    _get_search_index().start()
    logger.info("JobSearchIndex background indexer started.")
except Exception as _idx_exc:
    logger.warning("Could not start search indexer (non-fatal): %s", _idx_exc)

def _get_sid() -> str:
    if "sid" not in session:
        session["sid"] = str(uuid.uuid4())
    return session["sid"]


# ─── Config ───────────────────────────────────────────────────────────────────────

def _safe_redirect_url(target: str) -> str:
    """Return target if it's a safe same-origin URL, otherwise return the index URL."""
    if not target:
        return url_for("index")
    ref  = urlparse(request.host_url)
    test = urlparse(urljoin(request.host_url, target))
    if test.scheme in ("http", "https") and ref.netloc == test.netloc:
        return target
    logger.warning("Blocked open redirect attempt to: %r", target)
    return url_for("index")


def _artifact_share() -> Path:
    return Path(os.getenv("ARTIFACT_SHARE", r"C:\shared\builds"))


# ─── Param card builder ───────────────────────────────────────────────────────────

def _build_param_card(schema: list[dict], filled_params: dict, source_map: dict) -> list[dict]:
    """Assemble the param_card JSON sent to the frontend."""
    return [
        {
            "name":        p["name"],
            "type":        p["type"],
            "value":       filled_params.get(p["name"], p.get("default")),
            "choices":     p.get("choices", []),
            "description": p.get("description", ""),
            "source":      source_map.get(p["name"], "default"),
        }
        for p in schema
    ]


# ─── Override / merge logic ───────────────────────────────────────────────────────

def _merge_params(llm_params: dict, schema: list[dict]) -> tuple[dict, dict, list[str]]:
    """
    Merge LLM-extracted params with .env defaults and schema defaults.
    Priority: chat message (LLM) > .env DEFAULT_* > schema defaults.

    Returns (final_params, source_map, still_missing).
    """
    env_repo   = os.getenv("DEFAULT_REPO_URL", "").strip()
    env_branch = os.getenv("DEFAULT_BRANCH",   "").strip()

    final:   dict[str, object] = {}
    sources: dict[str, str]    = {}
    missing: list[str]         = []

    for p in schema:
        name           = p["name"]
        ptype          = p["type"]
        schema_default = p.get("default")
        llm_val        = llm_params.get(name)
        has_llm        = llm_val not in (None, "", [])

        if has_llm:
            final[name]   = llm_val
            sources[name] = "you provided"
        elif name == "GITHUB_URL" and env_repo:
            final[name]   = env_repo
            sources[name] = "default"
        elif name == "BRANCH" and env_branch:
            final[name]   = env_branch
            sources[name] = "default"
        elif schema_default is not None:
            final[name]   = schema_default
            sources[name] = "default"
        elif ptype == "BooleanParameterDefinition":
            final[name]   = False
            sources[name] = "default"
        else:
            missing.append(name)

    return final, sources, missing


def _override_notes(llm_params: dict) -> list[str]:
    """Return transparency notes when the dev overrides a .env default."""
    notes      = []
    env_repo   = os.getenv("DEFAULT_REPO_URL", "").strip()
    env_branch = os.getenv("DEFAULT_BRANCH",   "").strip()

    if env_repo and llm_params.get("GITHUB_URL") and llm_params["GITHUB_URL"] != env_repo:
        notes.append(f"Noted — using `{llm_params['GITHUB_URL']}` instead of default repo.")
    if env_branch and llm_params.get("BRANCH") and llm_params["BRANCH"] != env_branch:
        notes.append(f"Noted — using `{llm_params['BRANCH']}` instead of default branch.")
    return notes


# ─── Shared: schema fetch + LLM fill → param card ────────────────────────────────

def _schema_to_param_card(
    sid: str,
    job_name: str,
    user_message: str,
    context: list | None = None,
) -> dict:
    """
    Given a selected job_name, fetch its parameter schema, run LLM extraction,
    merge with defaults, and return either a param_card response or a clarification
    question if required fields are missing.

    Stores job_name + schema in _conv_sessions[sid] for use by param_submit.
    """
    from services import jenkins_client, llm_client
    from services.jenkins_client import JenkinsDownError, JenkinsError, JenkinsJobNotFoundError

    # Authorization check — before fetching any job details
    if not can_trigger_job(job_name):
        return {
            "reply":      f"⛔ You don't have permission to trigger **`{job_name}`**. "
                          "Contact your admin to request access.",
            "param_card": None,
            "job_card":   None,
        }

    # Fetch parameter schema
    try:
        schema = jenkins_client.get_job_parameters(job_name)
    except JenkinsJobNotFoundError:
        return {"reply": f"⚠️ Jenkins job `{job_name}` not found.", "param_card": None}
    except (JenkinsDownError, JenkinsError) as exc:
        return {"reply": f"⚠️ Jenkins error fetching parameters: {exc}", "param_card": None}

    # Persist for param_submit — include requested_artifacts from LLM
    _conv_sessions[sid] = {
        "job_name":            job_name,
        "schema":              schema,
        "last_message":        user_message,
        "requested_artifacts": [],   # filled after LLM extraction below
    }

    # LLM extraction
    try:
        llm_result = llm_client.parse_build_request(user_message, schema, context=context)
    except Exception as exc:
        logger.error("LLM extraction failed: %s", exc)
        llm_result = {
            "params":  {},
            "missing": [
                p["name"] for p in schema
                if not p.get("default") and p["type"] != "BooleanParameterDefinition"
            ],
            "requested_artifacts": [],
        }

    llm_params           = llm_result.get("params", {})
    requested_artifacts  = llm_result.get("requested_artifacts", [])

    # Persist requested_artifacts for the build thread
    _conv_sessions[sid]["requested_artifacts"] = requested_artifacts
    _save_conv_sessions()

    final_params, source_map, still_missing = _merge_params(llm_params, schema)

    # Ask for any required fields still missing
    if still_missing:
        missing_list = ", ".join(f"`{m}`" for m in still_missing)
        return {
            "reply": (
                f"Almost there — I still need: {missing_list}. "
                "Could you provide those?"
            ),
            "param_card": None,
            "job_card":   None,
        }

    param_card = _build_param_card(schema, final_params, source_map)
    reply_parts = [
        f"Using job **`{job_name}`**. "
        "Review the parameters below — click checkboxes, edit fields, "
        "or type comma-separated values. Click **Build Now** when ready."
    ]
    reply_parts.extend(_override_notes(llm_params))
    if requested_artifacts:
        names = ", ".join(f"`{f}`" for f in requested_artifacts)
        reply_parts.append(f"📦 Selective staging: only {names} will be copied.")

    return {
        "reply":      " ".join(reply_parts),
        "param_card": param_card,
        "job_card":   None,
    }


# ─── Job browser session helpers ─────────────────────────────────────────────────

def _browser_filters_from_session(sid: str) -> dict:
    """Return current job browser filter state for this session."""
    conv = _conv_sessions.get(sid, {})
    return dict(conv.get("job_browser_filters", {
        "q": "", "status": "", "hotfix": False,
        "repo": "", "branch": "",
        "folder": "", "job_type": "",
        "env_type": "", "aws_region": "", "cluster_name": "",
        "param_name": "", "param_value": "",
        "page": 1, "per_page": 6, "sort": "name",
    }))


# Default empty-filter state (used by clear-filters action)
_EMPTY_BROWSER_FILTERS: dict = {
    "q": "", "status": "", "hotfix": False,
    "repo": "", "branch": "",
    "folder": "", "job_type": "",
    "env_type": "", "aws_region": "", "cluster_name": "",
    "param_name": "", "param_value": "",
    "page": 1, "per_page": 6, "sort": "name",
}


def _build_job_browser_response(sid: str, filters: dict,
                                 jobs_all: list[dict] | None = None) -> dict:
    """
    Build the chat response with a job_browser card.

    Uses the JobSearchIndex when the index is ready (supports deep filters:
    repo URL, pipeline script text, config.xml metadata, env vars, etc.).
    Falls back to the in-memory filter_and_page_jobs() when the index is
    still building at startup, keeping the UI responsive immediately.
    """
    from services.jenkins_search import get_index as _get_idx
    from services import jenkins_client

    idx    = _get_idx()
    per_pg = int(filters.get("per_page", 6))

    if idx._ready.is_set():
        result = idx.search(
            q            = filters.get("q", ""),
            repo         = filters.get("repo", ""),
            branch       = filters.get("branch", ""),
            status       = filters.get("status", ""),
            hotfix       = bool(filters.get("hotfix", False)),
            folder       = filters.get("folder", ""),
            job_type     = filters.get("job_type", ""),
            param_name   = filters.get("param_name", ""),
            param_value  = filters.get("param_value", ""),
            env_type     = filters.get("env_type", ""),
            aws_region   = filters.get("aws_region", ""),
            cluster_name = filters.get("cluster_name", ""),
            page         = int(filters.get("page", 1)),
            per_page     = per_pg,
            sort         = filters.get("sort", "name"),
        )
    else:
        # Index not ready yet — fall back to flat list + legacy filter
        if jobs_all is None:
            try:
                jobs_all = jenkins_client.list_jobs()
            except Exception:
                jobs_all = []
        result = jenkins_client.filter_and_page_jobs(
            jobs_all,
            q              = filters.get("q", ""),
            status         = filters.get("status", ""),
            hotfix         = bool(filters.get("hotfix", False)),
            repo           = filters.get("repo", ""),
            branch         = filters.get("branch", ""),
            repo_param_key = "",
            page           = int(filters.get("page", 1)),
            per_page       = per_pg,
            sort           = filters.get("sort", "name"),
        )

    conv = _conv_sessions.setdefault(sid, {})
    conv["job_browser_filters"] = filters
    conv["job_browser_active"]  = True
    _save_conv_sessions()

    total    = result["total"]
    page     = result["page"]
    per_page = result["per_page"]
    start    = (page - 1) * per_page + 1
    end      = min(start + per_page - 1, total)
    act_f    = result["active_filters"]

    filter_strs: list[str] = []
    if act_f.get("status"):       filter_strs.append(f"status: **{act_f['status']}**")
    if act_f.get("hotfix"):       filter_strs.append("**hotfix only**")
    if act_f.get("q"):            filter_strs.append(f"search: **{act_f['q']}**")
    if act_f.get("repo"):         filter_strs.append(f"repo: **{act_f['repo']}**")
    if act_f.get("branch"):       filter_strs.append(f"branch: **{act_f['branch']}**")
    if act_f.get("folder"):       filter_strs.append(f"folder: **{act_f['folder']}**")
    if act_f.get("job_type"):     filter_strs.append(f"type: **{act_f['job_type']}**")
    if act_f.get("env_type"):     filter_strs.append(f"ENV_TYPE: **{act_f['env_type']}**")
    if act_f.get("aws_region"):   filter_strs.append(f"AWS_REGION: **{act_f['aws_region']}**")
    if act_f.get("cluster_name"): filter_strs.append(f"CLUSTER: **{act_f['cluster_name']}**")
    if act_f.get("param_name"):
        pv = f"={act_f['param_value']}" if act_f.get("param_value") else ""
        filter_strs.append(f"param: **{act_f['param_name']}{pv}**")

    if total == 0:
        active_str = ", ".join(filter_strs) if filter_strs else "none"
        reply = (
            f"No jobs found — active filters: {active_str}.\n"
            "Type `clear filters` to reset, or try a different search."
        )
    else:
        idx_st   = result.get("index_status") or {}
        idx_note = (
            f"  *(index: {idx_st['job_count']} jobs)*"
            if idx_st.get("job_count") else ""
        )
        count_str  = f"Showing **{start}–{end}** of **{total}** jobs{idx_note}"
        filter_str = f"  ·  {', '.join(filter_strs)}" if filter_strs else ""
        hint       = (
            "\nType: `next page`, `failed only`, `hotfix`, `pipeline jobs`, "
            "`jobs in folder X`, `sort by failed`, `clear filters`…"
        )
        reply = f"{count_str}{filter_str}.{hint}"

    return {
        "reply":      reply,
        "param_card": None,
        "job_card":   None,
        "job_browser": {
            "jobs":           result["jobs"],
            "total":          total,
            "page":           page,
            "per_page":       per_page,
            "total_pages":    result["total_pages"],
            "active_filters": act_f,
            "sort":           filters.get("sort", "name"),
            "index_status":   result.get("index_status"),
        },
    }


# ─── Query handler: Jenkins info / management actions ────────────────────────────

def _handle_query(sid: str, user_message: str, context: list | None = None) -> dict:
    """
    Handle Jenkins management / info queries.
    Detects the action via LLM (parse_jenkins_query) then dispatches.
    """
    from services import jenkins_client, llm_client
    from services.jenkins_client import (
        JenkinsDownError, JenkinsError, JenkinsJobNotFoundError,
    )

    def _err(msg):
        return {"reply": msg, "param_card": None, "job_card": None}

    def _job_picker(prompt, jobs):
        # Save the pending query action so _handle_job_select can resume it
        # instead of falling into the build flow
        _conv_sessions[sid] = {
            "job_name":       None,
            "last_message":   user_message,
            "available_jobs": jobs,
            "pending_query": {
                "action":         action,
                "build_number":   build_number,
                "build_number_b": build_number_b,
                "view_name":      view_name,
                "lines":          lines,
                "count":          count,
                "search_query":   search_query,
            },
        }
        _save_conv_sessions()
        return {
            "reply":      prompt,
            "param_card": None,
            "job_card":   [
                {"name": j["name"], "description": j.get("description", ""), "status": j.get("status", "")}
                for j in jobs
            ],
        }

    # ── Liveness ─────────────────────────────────────────────────────────────────
    if not jenkins_client.check_jenkins_alive():
        return _err(
            f"⚠️ Cannot reach Jenkins at "
            f"`{os.getenv('JENKINS_URL', 'http://localhost:8080')}`. "
            "Check that Jenkins is running."
        )

    # ── Gather context for the LLM parser ────────────────────────────────────────
    try:
        jobs = jenkins_client.list_jobs()
    except (JenkinsDownError, JenkinsError) as exc:
        return _err(f"⚠️ Could not list Jenkins jobs: {exc}")

    try:
        views = jenkins_client.list_views()
    except Exception:
        views = []

    job_names  = [j["name"] for j in jobs]
    view_names = [v["name"] for v in views]

    # ── Parse the query intent ────────────────────────────────────────────────────
    parsed       = llm_client.parse_jenkins_query(user_message, job_names, view_names, context=context or [])
    action       = parsed["action"]
    job_name       = parsed["job_name"]
    build_number   = parsed["build_number"]
    build_number_b = parsed.get("build_number_b")
    view_name      = parsed["view_name"]
    lines          = parsed["lines"] or 50
    count          = parsed.get("count") or 10
    search_query   = parsed["search_query"]

    logger.info("Query action=%r job=%r build=%s view=%r count=%d",
                action, re.sub(r'[\r\n\t]', '_', str(job_name)),
                build_number,
                re.sub(r'[\r\n\t]', '_', str(view_name)),
                count)

    # ════════════════════════════════════════════════════════════════════════════
    #  ACTION DISPATCH
    # ════════════════════════════════════════════════════════════════════════════

    # ── list_jobs / search_jobs → job browser ───────────────────────────────────
    if action in ("list_jobs", "search_jobs"):
        filters = _browser_filters_from_session(sid)
        if action == "search_jobs" and search_query:
            filters["q"] = search_query
        # Apply status filter from LLM (e.g. "list failed jobs" → status=FAILURE)
        if parsed.get("status"):
            filters["status"] = parsed["status"]
        filters["page"] = 1
        return _build_job_browser_response(sid, filters)

    # ── deep_search → extract rich structured filters then run index search ──────
    if action == "deep_search":
        from services.llm_client import parse_search_query
        sf      = parse_search_query(user_message, context=context)
        filters = _browser_filters_from_session(sid)
        # Merge extracted filter fields (only overwrite when LLM found something)
        _SF_KEYS = ("q", "repo", "branch", "status", "folder", "job_type",
                    "param_name", "param_value", "env_type", "aws_region", "cluster_name", "sort")
        for k in _SF_KEYS:
            if sf.get(k):
                filters[k] = sf[k]
        if sf.get("hotfix"):
            filters["hotfix"] = True
        filters["page"] = 1
        logger.info("deep_search filters: %s (source=%s)",
                    re.sub(r'[\r\n\t]', '_', str(sf)[:200]),
                    re.sub(r'[\r\n\t]', '_', str(sf.get("_source", ""))))
        return _build_job_browser_response(sid, filters)

    # ── index_status → show search-index health and stats ────────────────────────
    if action == "index_status":
        import datetime as _dt
        from services.jenkins_search import get_index as _get_idx
        idx = _get_idx()
        st  = idx.status()
        if st["building"]:
            return _err("🔄 Search index is currently being built — please wait a moment.")
        if st.get("built_at"):
            age       = st.get("age_s") or 0
            age_str   = f"{age // 60}m {age % 60}s" if age >= 60 else f"{age}s"
            nxt       = max(0, (st.get("ttl_s") or 600) - age)
            nxt_str   = f"{nxt // 60}m {nxt % 60}s" if nxt >= 60 else f"{nxt}s"
            err_note  = (
                f"\n• Errors: `{', '.join(st['errors'][:3])}`"
                if st.get("errors") else ""
            )
            return _err(
                f"📋 **Search index status:**\n"
                f"• **{st['job_count']}** jobs indexed\n"
                f"• Last built **{age_str}** ago\n"
                f"• Next refresh in **{nxt_str}** (TTL: {st.get('ttl_s', 600)}s)"
                f"{err_note}"
            )
        return _err(
            "⏳ Search index has not been built yet — it starts automatically at startup.\n"
            "Check that Jenkins is reachable and try again in a moment."
        )

    # ── list_views ───────────────────────────────────────────────────────────────
    if action == "list_views":
        if not views:
            return _err("No views found on this Jenkins instance.")
        lines_out = [f"**{len(views)} Jenkins views:**", ""]
        for v in views:
            lines_out.append(
                f"• **`{v['name']}`** — {v['job_count']} job(s)  "
                f"[🔗 Open]({v['url']})"
            )
        return _err("\n".join(lines_out))

    # ── list_jobs_in_view ────────────────────────────────────────────────────────
    if action == "list_jobs_in_view":
        if not view_name:
            view_list = "\n".join(f"• `{v['name']}` ({v['job_count']} jobs)" for v in views) if views else "none found"
            return _err(f"Which view? Available views:\n{view_list}")
        try:
            view_jobs = jenkins_client.list_jobs_in_view(view_name)
        except JenkinsJobNotFoundError:
            return _err(f"⚠️ View `{view_name}` not found. Try: `list views`")
        except Exception as exc:
            return _err(f"⚠️ Error fetching jobs for view `{view_name}`: {exc}")
        if not view_jobs:
            return _err(f"View **`{view_name}`** has no jobs.")
        lines_out = [f"**{len(view_jobs)} jobs in view `{view_name}`:**", ""]
        for j in view_jobs:
            icon = "✅" if "SUCCESS" in j["status"] else ("❌" if "FAIL" in j["status"] else "⚙️")
            desc = f" — {j['description']}" if j.get("description") else ""
            lines_out.append(f"• {icon} **`{j['name']}`**{desc}  *({j['status']})*")
        return _err("\n".join(lines_out))

    # ── stop_build ───────────────────────────────────────────────────────────────
    if action == "stop_build":
        if not job_name:
            return _job_picker("Which job's build would you like to stop?", jobs)
        if not build_number:
            build_number = jenkins_client.get_last_build_number(job_name)
            if not build_number:
                return _err(f"No builds found for `{job_name}`.")
        if not can_trigger_job(job_name):
            return _err(f"⛔ You don't have permission to stop builds of **`{job_name}`**.")
        ok = jenkins_client.abort_build(job_name, build_number)
        if ok:
            return _err(f"🛑 Stop signal sent to **`{job_name}`** build **#{build_number}**.")
        return _err(
            f"⚠️ Could not stop build #{build_number} of `{job_name}`. "
            "It may have already finished."
        )

    # ── who_triggered ────────────────────────────────────────────────────────────
    if action == "who_triggered":
        if not job_name:
            return _job_picker("Which job would you like to check?", jobs)
        try:
            info = jenkins_client.get_build_trigger(job_name, build_number)
        except Exception as exc:
            return _err(f"⚠️ Could not fetch build info for `{job_name}`: {exc}")
        ref   = f"build **#{info['build_number']}**" if info.get("build_number") else "last build"
        by    = f"**`{info['triggered_by']}`**" if info["triggered_by"] != "unknown" else "an automated process"
        cause = f" *(cause: {info['cause']})*" if info.get("cause") else ""
        link  = f"  [🔗 Open]({info['url']})" if info.get("url") else ""
        return _err(
            f"The {ref} of **`{job_name}`** was triggered by {by}{cause}.{link}"
        )

    # ── permissions ──────────────────────────────────────────────────────────────
    if action == "permissions":
        try:
            info = jenkins_client.get_current_user_info()
        except Exception as exc:
            return _err(f"⚠️ Could not fetch user info: {exc}")
        name  = info.get("display_name") or info.get("username") or "you"
        auths = info.get("authorities", [])
        if auths:
            roles = "\n".join(f"• `{a}`" for a in auths[:20])
            return _err(f"**{name}** has the following Jenkins authorities:\n{roles}")
        return _err(
            f"**{name}** — no authority list returned by Jenkins (`/me/api/json`). "
            "Your roles are configured in Jenkins → Manage Jenkins → Security."
        )

    # ── list_artifacts ───────────────────────────────────────────────────────────
    if action == "list_artifacts":
        if not job_name:
            return _job_picker("Which job's artifacts would you like to see?", jobs)
        try:
            art_data = jenkins_client.list_build_artifacts(job_name, build_number)
        except Exception as exc:
            return _err(f"⚠️ Could not fetch artifacts for `{job_name}`: {exc}")
        artifacts = art_data["artifacts"]
        bn        = art_data["build_number"]
        ref_str   = f"build **#{bn}**" if bn else "last successful build"
        if not artifacts:
            return _err(
                f"No artifacts found for **`{job_name}`** {ref_str}. "
                "Artifacts must be archived by the Jenkins job (Post-build Actions → Archive)."
            )
        lines_out = [f"📦 **{len(artifacts)} artifact(s)** from **`{job_name}`** {ref_str}:", ""]
        for art in artifacts:
            lines_out.append(
                f"• [`{art['name']}`]({art['download_url']}) "
                f"[⬇ Download]({art['download_url']})"
            )
        return _err("\n".join(lines_out))

    # ── console_log ──────────────────────────────────────────────────────────────
    if action == "console_log":
        if not job_name:
            return _job_picker(
                "Which job's console log would you like to see?\n"
                "*(You can also specify a build number, e.g. "
                "\"show console log for DOTNET service build 5\")*",
                jobs,
            )
        lines_count = max(10, min(lines, 200))   # clamp 10–200
        try:
            log_data = jenkins_client.get_console_log_tail(job_name, build_number, lines_count)
        except Exception as exc:
            return _err(f"⚠️ Could not fetch console log for `{job_name}`: {exc}")

        actual_bn  = log_data["build_number"]
        log_lines  = log_data["lines"]
        total      = log_data["total_lines"]
        result     = log_data.get("result")

        if not log_lines:
            return _err(
                f"No console output found for **`{job_name}`** "
                f"{'build #' + str(actual_bn) if actual_bn else '(last build)'}."
            )
        result_tag = f"  *(result: **{result}**)*" if result else ""
        header     = (
            f"📋 Last **{len(log_lines)}** of **{total}** lines — "
            f"**`{job_name}`** build **#{actual_bn}**{result_tag}:\n```\n"
        )
        return _err(header + "\n".join(log_lines) + "\n```")

    # ── sftp_path ────────────────────────────────────────────────────────────────
    if action == "sftp_path":
        if not job_name:
            return _job_picker("Which job should I check for SFTP upload paths?", jobs)
        try:
            log_data = jenkins_client.get_console_log_tail(job_name, build_number, 200)
        except Exception as exc:
            return _err(f"⚠️ Could not fetch console log for `{job_name}`: {exc}")
        paths = jenkins_client.find_sftp_uploads(log_data["lines"])
        bn    = log_data["build_number"]
        if not paths:
            return _err(
                f"No SFTP/SCP upload paths found in **`{job_name}`** build **#{bn}**. "
                "The job may not perform SFTP uploads, or the pattern was not recognised."
            )
        path_list = "\n".join(f"• `{p}`" for p in paths)
        return _err(
            f"📤 SFTP upload path(s) in **`{job_name}`** build **#{bn}**:\n{path_list}"
        )

    # ── analyze_failure ──────────────────────────────────────────────────────────
    if action == "analyze_failure":
        if not job_name:
            return _job_picker("Which job's failure would you like me to analyse?", jobs)
        try:
            log_data = jenkins_client.get_console_log_tail(job_name, build_number, 50)
        except Exception as exc:
            return _err(f"⚠️ Could not fetch console log for `{job_name}`: {exc}")

        bn        = log_data["build_number"]
        result    = log_data.get("result", "FAILURE")
        log_lines = log_data["lines"]

        if not log_lines:
            return _err(
                f"No console output found for **`{job_name}`** "
                f"{'build #' + str(bn) if bn else '(last build)'}."
            )

        # Extract the 20 most error-relevant lines for display
        error_lines = [
            l for l in log_lines
            if any(kw in l.lower() for kw in ("error", "fail", "exception", "fatal", "killed", "aborted"))
        ][-20:] or log_lines[-20:]

        # LLM analysis with 20s timeout, regex fallback built-in
        from services.llm_client import llm_analyze_build_failure
        diag = llm_analyze_build_failure(log_lines, result or "FAILURE")

        header   = (
            f"🔍 **Failure analysis** — **`{job_name}`** build **#{bn}**  "
            f"*(result: {result or 'FAILURE'})*\n\n"
        )
        analysis = ""
        if diag.get("cause"):
            analysis += f"**Cause:** {diag['cause']}\n"
        if diag.get("suggestion"):
            analysis += f"**Fix:** {diag['suggestion']}\n"
        analysis += f"*(source: {diag.get('source', 'regex')})*\n\n"

        log_block = (
            f"**Last {len(error_lines)} relevant log lines:**\n"
            f"```\n" + "\n".join(error_lines) + "\n```"
        )
        return _err(header + analysis + log_block)

    # ── list_running_builds ───────────────────────────────────────────────────────
    if action == "list_running_builds":
        try:
            running = jenkins_client.list_running_builds()
        except Exception as exc:
            return _err(f"⚠️ Could not fetch running builds: {exc}")
        if not running:
            return _err("✅ No builds are currently running.")
        lines_out = [f"**{len(running)} build(s) currently running:**", ""]
        for b in running:
            elapsed = f"{b['elapsed_s']//60}m {b['elapsed_s']%60}s" if b['elapsed_s'] >= 60 else f"{b['elapsed_s']}s"
            lines_out.append(f"• **`{b['job_name']}`** — build **#{b['build_number']}**  *(running for {elapsed})*  [🔗]({b['url']})")
        return _err("\n".join(lines_out))

    # ── list_build_history ────────────────────────────────────────────────────────
    if action == "list_build_history":
        if not job_name:
            return _job_picker("Which job's build history would you like to see?", jobs)
        try:
            history = jenkins_client.list_build_history(job_name, count)
        except Exception as exc:
            return _err(f"⚠️ Could not fetch build history for `{job_name}`: {exc}")
        if not history:
            return _err(f"No builds found for **`{job_name}`**.")
        import datetime as _dt
        lines_out = [f"**Last {len(history)} builds — `{job_name}`:**", ""]
        for b in history:
            icon = "✅" if b["result"] == "SUCCESS" else ("❌" if b["result"] == "FAILURE" else "⚠️")
            dur  = f"{b['duration_s']//60}m {b['duration_s']%60}s" if b["duration_s"] else "—"
            ts   = _dt.datetime.fromtimestamp(b["timestamp_s"]).strftime("%Y-%m-%d %H:%M") if b["timestamp_s"] else "—"
            lines_out.append(f"• {icon} **#{b['number']}** — {b['result']}  `{dur}`  {ts}  [🔗]({b['url']})")
        return _err("\n".join(lines_out))

    # ── retry_build ───────────────────────────────────────────────────────────────
    if action == "retry_build":
        if not job_name:
            return _job_picker("Which job's build would you like to retry?", jobs)
        if not build_number:
            hist = jenkins_client.list_build_history(job_name, 1)
            build_number = hist[0]["number"] if hist else None
            if not build_number:
                return _err(f"No builds found for `{job_name}`.")
        if not can_trigger_job(job_name):
            return _err(f"⛔ You don't have permission to trigger builds of **`{job_name}`**.")
        ok = jenkins_client.retry_build(job_name, build_number)
        if ok:
            return _err(f"🔁 Rebuild triggered for **`{job_name}`** — repeating build **#{build_number}**.")
        return _err(
            f"⚠️ Could not retry build #{build_number} of `{job_name}`. "
            "The Rebuild Plugin may not be installed in Jenkins."
        )

    # ── list_queue ────────────────────────────────────────────────────────────────
    if action == "list_queue":
        try:
            items = jenkins_client.list_queue()
        except Exception as exc:
            return _err(f"⚠️ Could not fetch Jenkins queue: {exc}")
        if not items:
            return _err("✅ Jenkins queue is empty — no builds waiting.")
        lines_out = [f"**{len(items)} build(s) in the Jenkins queue:**", ""]
        for item in items:
            wait = f"{item['in_queue_s']//60}m {item['in_queue_s']%60}s" if item['in_queue_s'] >= 60 else f"{item['in_queue_s']}s"
            why  = f"  *({item['why']})*" if item.get("why") else ""
            stuck = "  ⚠️ STUCK" if item.get("stuck") else ""
            lines_out.append(f"• **`{item['job_name']}`** — waiting {wait}{why}{stuck}")
        return _err("\n".join(lines_out))

    # ── list_agents ───────────────────────────────────────────────────────────────
    if action == "list_agents":
        try:
            agents = jenkins_client.list_agents()
        except Exception as exc:
            return _err(f"⚠️ Could not fetch Jenkins agents: {exc}")
        if not agents:
            return _err("No Jenkins agents/nodes found.")
        lines_out = [f"**{len(agents)} Jenkins agent(s):**", ""]
        for a in agents:
            status = "🟢 Online" if a["online"] else "🔴 Offline"
            if a.get("temp_offline"):
                status = "🟡 Temporarily offline"
            idle = "idle" if a["idle"] else f"busy ({a['executors']} executors)"
            reason = f"  *Reason: {a['offline_reason']}*" if a.get("offline_reason") and not a["online"] else ""
            lines_out.append(f"• **`{a['name']}`** — {status}  {idle}{reason}")
        return _err("\n".join(lines_out))

    # ── list_build_changes ────────────────────────────────────────────────────────
    if action == "list_build_changes":
        if not job_name:
            return _job_picker("Which job's git changes would you like to see?", jobs)
        try:
            data = jenkins_client.list_build_changes(job_name, build_number)
        except Exception as exc:
            return _err(f"⚠️ Could not fetch changes for `{job_name}`: {exc}")
        changes = data["changes"]
        bn      = data["build_number"]
        if not changes:
            return _err(f"No git changes recorded for **`{job_name}`** build **#{bn}**.")
        lines_out = [f"**{len(changes)} commit(s) in `{job_name}` build #{bn}:**", ""]
        for c in changes:
            short_msg = c["message"][:72] + ("…" if len(c["message"]) > 72 else "")
            files_str = f"  *({c['affected_files']} file(s))*" if c["affected_files"] else ""
            commit    = f"`{c['commit_id']}`  " if c["commit_id"] else ""
            lines_out.append(f"• {commit}**{c['author']}** — {short_msg}{files_str}")
        return _err("\n".join(lines_out))

    # ── compare_builds ────────────────────────────────────────────────────────────
    if action == "compare_builds":
        if not job_name:
            return _job_picker("Which job's builds would you like to compare?", jobs)
        if not build_number or not build_number_b:
            return _err(
                f"Please specify two build numbers, e.g.:\n"
                f"`compare build 5 and 6 of {job_name or 'DOTNET service'}`"
            )
        try:
            cmp = jenkins_client.compare_builds(job_name, build_number, build_number_b)
        except Exception as exc:
            return _err(f"⚠️ Could not compare builds for `{job_name}`: {exc}")
        a = cmp["build_a"]
        b = cmp["build_b"]
        icon_a = "✅" if a["result"] == "SUCCESS" else "❌"
        icon_b = "✅" if b["result"] == "SUCCESS" else "❌"
        dur_a  = f"{a['duration_s']//60}m {a['duration_s']%60}s"
        dur_b  = f"{b['duration_s']//60}m {b['duration_s']%60}s"
        lines_out = [
            f"**Build comparison — `{job_name}`:**", "",
            f"| | Build #{a['number']} | Build #{b['number']} |",
            f"|---|---|---|",
            f"| Result    | {icon_a} {a['result']} | {icon_b} {b['result']} |",
            f"| Duration  | {dur_a} | {dur_b} |",
            f"| Changes   | {a['changes']} commit(s) | {b['changes']} commit(s) |",
        ]
        return _err("\n".join(lines_out))

    # ── search_failed_builds ──────────────────────────────────────────────────────
    if action == "search_failed_builds":
        if not job_name:
            return _job_picker("Which job's failure history would you like to see?", jobs)
        try:
            failed = jenkins_client.search_failed_builds(job_name, count)
        except Exception as exc:
            return _err(f"⚠️ Could not fetch failure history for `{job_name}`: {exc}")
        if not failed:
            return _err(f"✅ No failures found in the last {count} builds of **`{job_name}`**.")
        import datetime as _dt
        lines_out = [f"**{len(failed)} failure(s) in last {count} builds — `{job_name}`:**", ""]
        for b in failed:
            icon = "❌" if b["result"] == "FAILURE" else "⚠️"
            dur  = f"{b['duration_s']//60}m {b['duration_s']%60}s" if b["duration_s"] else "—"
            ts   = _dt.datetime.fromtimestamp(b["timestamp_s"]).strftime("%Y-%m-%d %H:%M") if b["timestamp_s"] else "—"
            lines_out.append(f"• {icon} **#{b['number']}** — {b['result']}  `{dur}`  {ts}  [🔗]({b['url']})")
        return _err("\n".join(lines_out))

    # ── get_jenkins_info ──────────────────────────────────────────────────────────
    if action == "get_jenkins_info":
        try:
            info = jenkins_client.get_jenkins_info()
        except Exception as exc:
            return _err(f"⚠️ Could not fetch Jenkins info: {exc}")
        busy     = info["busy_executors"]
        total    = info["total_executors"]
        offline  = info["offline_nodes"]
        url_link = f"  [🔗 Open Jenkins]({info['url']})" if info.get("url") else ""
        lines_out = [
            f"**Jenkins Server Info**{url_link}", "",
            f"• **Version:** `{info['version']}`",
            f"• **Executors:** {busy}/{total} busy",
            f"• **Offline nodes:** {offline}",
        ]
        if info.get("description"):
            lines_out.append(f"• **Description:** {info['description']}")
        return _err("\n".join(lines_out))

    # ── list_plugins ──────────────────────────────────────────────────────────────
    if action == "list_plugins":
        try:
            plugins = jenkins_client.list_plugins()
        except Exception as exc:
            return _err(f"⚠️ Could not fetch plugins: {exc}")
        if not plugins:
            return _err("No plugins found (may need Overall/Administer permission).")
        active   = [p for p in plugins if p["active"]]
        updates  = [p for p in plugins if p["has_update"]]
        lines_out = [
            f"**{len(plugins)} plugin(s) installed  "
            f"({len(active)} active, {len(updates)} update(s) available):**", ""
        ]
        for p in plugins[:40]:   # cap at 40 to keep response manageable
            upd = " 🔄" if p["has_update"] else ""
            act = "✅" if p["active"] else "⬜"
            lines_out.append(f"• {act} **{p['name']}** `{p['version']}`{upd}")
        if len(plugins) > 40:
            lines_out.append(f"*…and {len(plugins)-40} more*")
        return _err("\n".join(lines_out))

    # ── unknown ──────────────────────────────────────────────────────────────────
    return _err(
        "I didn't quite understand that Jenkins request. Here's what I can help with:\n\n"
        "**Build operations:**\n"
        "• `list jobs` / `list views` / `jobs in <view> view`\n"
        "• `search jobs <keyword>`\n"
        "• `who triggered the last build of <job>`\n"
        "• `show build history of <job> [last N]`\n"
        "• `show running builds`\n"
        "• `retry build <N> of <job>`\n"
        "• `stop build <N> of <job>`\n\n"
        "**Information:**\n"
        "• `show console log for <job> [build N] [last N lines]`\n"
        "• `show artifacts for <job> [build N]`\n"
        "• `show git commits for <job> [build N]`\n"
        "• `compare build <N> and <M> of <job>`\n"
        "• `show failed builds for <job>`\n"
        "• `show Jenkins queue`\n"
        "• `show Jenkins agents`\n"
        "• `Jenkins version`\n"
        "• `show installed plugins`\n\n"
        "**Analysis:**\n"
        "• `analyze why <job> failed [build N]`\n"
        "• `show SFTP upload paths for <job>`\n"
        "• `my permissions`"
    )


# ─── Phase 1: message → job discovery → job select or param card ──────────────────

def _handle_message(sid: str, user_message: str, context: list | None = None) -> dict:
    from services import jenkins_client, llm_client
    from services.jenkins_client import JenkinsDownError, JenkinsError

    ctx = context or []   # prior conversation turns for LLM

    # ── Fast intent check — skip Jenkins entirely for general chat ──────────────
    intent = llm_client.detect_intent(user_message)
    if intent == "chat":
        reply = llm_client.general_chat_response(user_message, context=ctx)
        return {"reply": reply, "param_card": None, "job_card": None}

    # ── Job browser is active — user is navigating / filtering ──────────────────
    conv = _conv_sessions.get(sid, {})
    if conv.get("job_browser_active") and intent != "build":
        from services.llm_client import parse_job_filter_update
        filters = _browser_filters_from_session(sid)
        update  = parse_job_filter_update(user_message, filters)
        act     = update.get("action", "filter")

        if act == "select" and update.get("select_job"):
            conv["job_browser_active"] = False
            audit_logger.info("JOB_BROWSER_SELECT  user=%s  job=%s",
                              _sl(current_user()["username"] if current_user() else "unknown"),
                              _sl(update["select_job"]))
            return _schema_to_param_card(sid, update["select_job"], user_message)

        if act == "clear":
            filters = dict(_EMPTY_BROWSER_FILTERS)
        elif act == "next":
            filters["page"] = filters.get("page", 1) + 1
        elif act == "prev":
            filters["page"] = max(1, filters.get("page", 1) - 1)
        elif act.startswith("page_"):
            try:
                filters["page"] = int(act.split("_")[1])
            except (ValueError, IndexError):
                pass
        else:
            if update.get("status"):       filters["status"]       = update["status"]
            if update.get("hotfix"):       filters["hotfix"]       = update["hotfix"]
            if update.get("q"):            filters["q"]            = update["q"]
            if update.get("repo"):         filters["repo"]         = update["repo"]
            if update.get("branch"):       filters["branch"]       = update["branch"]
            if update.get("sort"):         filters["sort"]         = update["sort"]
            if update.get("folder"):       filters["folder"]       = update["folder"]
            if update.get("job_type"):     filters["job_type"]     = update["job_type"]
            if update.get("env_type"):     filters["env_type"]     = update["env_type"]
            if update.get("aws_region"):   filters["aws_region"]   = update["aws_region"]
            if update.get("cluster_name"): filters["cluster_name"] = update["cluster_name"]
            if update.get("param_name"):   filters["param_name"]   = update["param_name"]
            if update.get("param_value"):  filters["param_value"]  = update["param_value"]
            filters["page"] = 1

        return _build_job_browser_response(sid, filters)

    if intent == "query":
        return _handle_query(sid, user_message, ctx)

    # ── Build intent → proceed with Jenkins flow ────────────────────────────────
    if not jenkins_client.check_jenkins_alive():
        return {
            "reply": (
                f"⚠️ Cannot reach Jenkins at "
                f"`{os.getenv('JENKINS_URL', 'http://localhost:8080')}`. "
                "Please make sure Jenkins is running and try again."
            ),
            "param_card": None,
            "job_card":   None,
        }

    # ── Discover available jobs ─────────────────────────────────────────────────
    try:
        jobs = jenkins_client.list_jobs()
    except (JenkinsDownError, JenkinsError) as exc:
        return {"reply": f"⚠️ Could not list Jenkins jobs: {exc}", "param_card": None, "job_card": None}

    if not jobs:
        return {
            "reply": (
                "⚠️ No Jenkins jobs found. "
                "Check that JENKINS_USER has permission to read jobs, "
                "and that JENKINS_JOBS (if set) matches at least one job name."
            ),
            "param_card": None,
            "job_card":   None,
        }

    # ── Single job — skip LLM selection entirely ────────────────────────────────
    if len(jobs) == 1:
        return _schema_to_param_card(sid, jobs[0]["name"], user_message, context=ctx)

    # ── Vague message — skip LLM, show job picker immediately ───────────────────
    # If the message is just "build" / "trigger" / "deploy" with no repo URL,
    # branch, or job hint, asking the LLM to pick a job will give a random guess.
    # Show the picker directly and ask the user to provide details.
    _HAS_DETAILS_RE = re.compile(
        r'https?://|/hotfix/|/release/|/feature/|/fix/'
        r'|branch\s+\S|from\s+https?'
        r'|\b(hotfix|release|feature|fix)/\S',
        re.IGNORECASE,
    )
    _is_vague = (
        len(user_message.strip().split()) <= 3
        and not _HAS_DETAILS_RE.search(user_message)
    )
    if _is_vague:
        _conv_sessions[sid] = {
            "job_name":       None,
            "last_message":   user_message,
            "available_jobs": jobs,
        }
        _save_conv_sessions()
        return {
            "reply": (
                "Which job should I trigger?\n\n"
                "Or type a full request like:\n"
                "`build hotfix/PAY-1 from https://github.com/acme/repo`"
            ),
            "param_card": None,
            "job_card": [
                {"name": j["name"], "description": j.get("description", ""), "status": j.get("status", "")}
                for j in jobs
            ],
        }

    # ── Multiple jobs — let LLM pick ────────────────────────────────────────────
    selection = llm_client.select_job(user_message, jobs, context=ctx)
    job_name  = selection.get("job_name")
    confidence = selection.get("confidence", "low")

    if job_name and confidence == "high":
        logger.info("LLM selected job %r with high confidence: %s", job_name, selection.get("reason"))
        return _schema_to_param_card(sid, job_name, user_message, context=ctx)

    # ── Ambiguous — show job picker card ────────────────────────────────────────
    logger.info(
        "LLM could not confidently select a job (confidence=%s, job=%r). Showing picker.",
        confidence, job_name,
    )

    # Store message in session so it's available after the dev picks a job
    _conv_sessions[sid] = {
        "job_name":        None,
        "last_message":    user_message,
        "available_jobs":  jobs,
    }
    _save_conv_sessions()

    hint = ""
    if job_name:
        hint = f" (I think it might be **`{job_name}`**, but I'm not sure)"

    return {
        "reply":      f"I found {len(jobs)} jobs{hint}. Which one should I trigger?",
        "param_card": None,
        "job_card":   [
            {
                "name":        j["name"],
                "description": j.get("description", ""),
                "status":      j.get("status", ""),
            }
            for j in jobs
        ],
    }


# ─── Phase 1b: dev picked a job from the job picker ──────────────────────────────

def _resume_query(sid: str, job_name: str, pending: dict, user_message: str) -> dict:
    """
    Resume a query action after the user picked a job from the picker.
    Called by _handle_job_select when pending_query is stored in the session.
    """
    from services import jenkins_client

    def _err(msg): return {"reply": msg, "param_card": None, "job_card": None}

    action       = pending.get("action")
    build_number = pending.get("build_number")
    lines        = pending.get("lines") or 50

    # ── who_triggered ────────────────────────────────────────────────────────────
    if action == "who_triggered":
        try:
            info = jenkins_client.get_build_trigger(job_name, build_number)
        except Exception as exc:
            return _err(f"⚠️ Could not fetch build info for `{job_name}`: {exc}")
        ref   = f"build **#{info['build_number']}**" if info.get("build_number") else "last build"
        by    = f"**`{info['triggered_by']}`**" if info["triggered_by"] != "unknown" else "an automated process"
        cause = f" *(cause: {info['cause']})*" if info.get("cause") else ""
        link  = f"  [🔗 Open]({info['url']})" if info.get("url") else ""
        return _err(f"The {ref} of **`{job_name}`** was triggered by {by}{cause}.{link}")

    # ── stop_build ───────────────────────────────────────────────────────────────
    if action == "stop_build":
        if not build_number:
            build_number = jenkins_client.get_last_build_number(job_name)
            if not build_number:
                return _err(f"No builds found for `{job_name}`.")
        if not can_trigger_job(job_name):
            return _err(f"⛔ You don't have permission to stop builds of **`{job_name}`**.")
        ok = jenkins_client.abort_build(job_name, build_number)
        if ok:
            return _err(f"🛑 Stop signal sent to **`{job_name}`** build **#{build_number}**.")
        return _err(
            f"⚠️ Could not stop build #{build_number} of `{job_name}`. "
            "It may have already finished."
        )

    # ── list_artifacts ────────────────────────────────────────────────────────────
    if action == "list_artifacts":
        try:
            art_data = jenkins_client.list_build_artifacts(job_name, build_number)
        except Exception as exc:
            return _err(f"⚠️ Could not fetch artifacts for `{job_name}`: {exc}")
        artifacts = art_data["artifacts"]
        bn        = art_data["build_number"]
        ref_str   = f"build **#{bn}**" if bn else "last successful build"
        if not artifacts:
            return _err(f"No artifacts found for **`{job_name}`** {ref_str}.")
        lines_out = [f"📦 **{len(artifacts)} artifact(s)** from **`{job_name}`** {ref_str}:", ""]
        for art in artifacts:
            lines_out.append(
                f"• [`{art['name']}`]({art['download_url']}) "
                f"[⬇ Download]({art['download_url']})"
            )
        return _err("\n".join(lines_out))

    # ── console_log ──────────────────────────────────────────────────────────────
    if action == "console_log":
        lines_count = max(10, min(lines, 200))
        try:
            log_data = jenkins_client.get_console_log_tail(job_name, build_number, lines_count)
        except Exception as exc:
            return _err(f"⚠️ Could not fetch console log for `{job_name}`: {exc}")
        actual_bn  = log_data["build_number"]
        log_lines  = log_data["lines"]
        total      = log_data["total_lines"]
        result     = log_data.get("result")
        if not log_lines:
            return _err(f"No console output found for **`{job_name}`**.")
        result_tag = f"  *(result: **{result}**)*" if result else ""
        header = (
            f"📋 Last **{len(log_lines)}** of **{total}** lines — "
            f"**`{job_name}`** build **#{actual_bn}**{result_tag}:\n```\n"
        )
        return _err(header + "\n".join(log_lines) + "\n```")

    # ── sftp_path ─────────────────────────────────────────────────────────────────
    if action == "sftp_path":
        try:
            log_data = jenkins_client.get_console_log_tail(job_name, build_number, 200)
        except Exception as exc:
            return _err(f"⚠️ Could not fetch console log for `{job_name}`: {exc}")
        paths = jenkins_client.find_sftp_uploads(log_data["lines"])
        bn    = log_data["build_number"]
        if not paths:
            return _err(
                f"No SFTP/SCP upload paths found in **`{job_name}`** build **#{bn}**. "
                "The job may not perform SFTP uploads."
            )
        path_list = "\n".join(f"• `{p}`" for p in paths)
        return _err(f"📤 SFTP upload path(s) in **`{job_name}`** build **#{bn}**:\n{path_list}")

    # ── analyze_failure ───────────────────────────────────────────────────────────
    if action == "analyze_failure":
        try:
            log_data = jenkins_client.get_console_log_tail(job_name, build_number, 50)
        except Exception as exc:
            return _err(f"⚠️ Could not fetch console log for `{job_name}`: {exc}")
        bn        = log_data["build_number"]
        result    = log_data.get("result", "FAILURE")
        log_lines = log_data["lines"]
        if not log_lines:
            return _err(f"No console output found for **`{job_name}`** build **#{bn}**.")
        error_lines = [
            l for l in log_lines
            if any(kw in l.lower() for kw in ("error", "fail", "exception", "fatal", "killed", "aborted"))
        ][-20:] or log_lines[-20:]
        from services.llm_client import llm_analyze_build_failure
        diag = llm_analyze_build_failure(log_lines, result or "FAILURE")
        header   = (
            f"🔍 **Failure analysis** — **`{job_name}`** build **#{bn}**  "
            f"*(result: {result or 'FAILURE'})*\n\n"
        )
        analysis = ""
        if diag.get("cause"):      analysis += f"**Cause:** {diag['cause']}\n"
        if diag.get("suggestion"): analysis += f"**Fix:** {diag['suggestion']}\n"
        analysis += f"*(source: {diag.get('source', 'regex')})*\n\n"
        log_block = (
            f"**Last {len(error_lines)} relevant log lines:**\n```\n"
            + "\n".join(error_lines) + "\n```"
        )
        return _err(header + analysis + log_block)

    # ── list_build_history (via picker) ──────────────────────────────────────────
    if action == "list_build_history":
        try:
            history = jenkins_client.list_build_history(job_name, pending.get("count") or 10)
        except Exception as exc:
            return _err(f"⚠️ Could not fetch build history for `{job_name}`: {exc}")
        if not history:
            return _err(f"No builds found for **`{job_name}`**.")
        import datetime as _dt
        lines_out = [f"**Last {len(history)} builds — `{job_name}`:**", ""]
        for b in history:
            icon = "✅" if b["result"] == "SUCCESS" else ("❌" if b["result"] == "FAILURE" else "⚠️")
            dur  = f"{b['duration_s']//60}m {b['duration_s']%60}s" if b["duration_s"] else "—"
            ts   = _dt.datetime.fromtimestamp(b["timestamp_s"]).strftime("%Y-%m-%d %H:%M") if b["timestamp_s"] else "—"
            lines_out.append(f"• {icon} **#{b['number']}** — {b['result']}  `{dur}`  {ts}  [🔗]({b['url']})")
        return _err("\n".join(lines_out))

    # ── retry_build (via picker) ──────────────────────────────────────────────────
    if action == "retry_build":
        bn = build_number
        if not bn:
            hist = jenkins_client.list_build_history(job_name, 1)
            bn   = hist[0]["number"] if hist else None
        if not bn:
            return _err(f"No builds found for `{job_name}`.")
        if not can_trigger_job(job_name):
            return _err(f"⛔ You don't have permission to trigger builds of **`{job_name}`**.")
        ok = jenkins_client.retry_build(job_name, bn)
        if ok:
            return _err(f"🔁 Rebuild triggered for **`{job_name}`** — repeating build **#{bn}**.")
        return _err(f"⚠️ Could not retry build #{bn} of `{job_name}`. The Rebuild Plugin may not be installed.")

    # ── list_build_changes (via picker) ───────────────────────────────────────────
    if action == "list_build_changes":
        try:
            data = jenkins_client.list_build_changes(job_name, build_number)
        except Exception as exc:
            return _err(f"⚠️ Could not fetch changes for `{job_name}`: {exc}")
        changes = data["changes"]
        bn      = data["build_number"]
        if not changes:
            return _err(f"No git changes recorded for **`{job_name}`** build **#{bn}**.")
        lines_out = [f"**{len(changes)} commit(s) in `{job_name}` build #{bn}:**", ""]
        for c in changes:
            short_msg = c["message"][:72] + ("…" if len(c["message"]) > 72 else "")
            files_str = f"  *({c['affected_files']} file(s))*" if c["affected_files"] else ""
            commit    = f"`{c['commit_id']}`  " if c["commit_id"] else ""
            lines_out.append(f"• {commit}**{c['author']}** — {short_msg}{files_str}")
        return _err("\n".join(lines_out))

    # ── search_failed_builds (via picker) ─────────────────────────────────────────
    if action == "search_failed_builds":
        cnt = pending.get("count") or 20
        try:
            failed = jenkins_client.search_failed_builds(job_name, cnt)
        except Exception as exc:
            return _err(f"⚠️ Could not fetch failure history for `{job_name}`: {exc}")
        if not failed:
            return _err(f"✅ No failures found in the last {cnt} builds of **`{job_name}`**.")
        import datetime as _dt
        lines_out = [f"**{len(failed)} failure(s) in last {cnt} builds — `{job_name}`:**", ""]
        for b in failed:
            icon = "❌" if b["result"] == "FAILURE" else "⚠️"
            dur  = f"{b['duration_s']//60}m {b['duration_s']%60}s" if b["duration_s"] else "—"
            ts   = _dt.datetime.fromtimestamp(b["timestamp_s"]).strftime("%Y-%m-%d %H:%M") if b["timestamp_s"] else "—"
            lines_out.append(f"• {icon} **#{b['number']}** — {b['result']}  `{dur}`  {ts}  [🔗]({b['url']})")
        return _err("\n".join(lines_out))

    return _err(f"⚠️ Could not resume query action `{action}`. Please try again.")


def _handle_job_select(sid: str, job_name: str) -> dict:
    conv    = _conv_sessions.get(sid, {})
    pending = conv.get("pending_query")

    if pending:
        # User picked a job to answer a query — not to trigger a build
        msg = conv.get("last_message", "")
        _conv_sessions.pop(sid, None)   # clear so a subsequent build request starts fresh
        _save_conv_sessions()
        return _resume_query(sid, job_name, pending, msg)

    # Normal build flow
    user_message = conv.get("last_message", "")
    return _schema_to_param_card(sid, job_name, user_message)


# ─── Phase 2: background build thread ────────────────────────────────────────────

def _update_job(job_id: str, status: str, message: str, done: bool = False, **extra) -> None:
    _build_jobs[job_id].update({"status": status, "message": message, "done": done, **extra})
    logger.info("[build:%s] %s — %s", job_id[:8], status, message)


def _copy_artifacts(
    job_name: str,
    build_number: int,
    requested: list[str] | None = None,
) -> dict:
    """
    Copy build artifacts from Jenkins to the configured share folder.

    Parameters
    ----------
    requested : Optional list of filenames the developer asked for.
                If non-empty, only those files are copied (case-insensitive match).
                If None / empty, all artifacts are copied.

    Returns
    -------
    {
        "dest":      Path,           # destination folder
        "staged":    [{"name":..., "path":...}],   # files successfully copied
        "not_found": [str, ...],     # requested files not found in build artifacts
        "selective": bool,           # True if specific files were requested
    }
    """
    dest = _artifact_share() / str(build_number)

    staged:    list[dict] = []
    not_found: list[str]  = []
    selective              = bool(requested)

    try:
        dest.mkdir(parents=True, exist_ok=True)
        from services.jenkins_client import _get as _jenkins_get
        api_url   = (
            f"{os.getenv('JENKINS_URL', 'http://localhost:8080')}"
            f"/job/{job_name}/{build_number}/api/json"
            f"?tree=artifacts[fileName,relativePath]"
        )
        all_artifacts = _jenkins_get(api_url).json().get("artifacts", [])

        # Determine which artifacts to copy
        if selective:
            req_lower = {r.lower(): r for r in requested}
            to_copy   = [
                a for a in all_artifacts
                if a["fileName"].lower() in req_lower
            ]
            # Find any requested files not present in build output
            found_lower = {a["fileName"].lower() for a in to_copy}
            not_found   = [
                req_lower[r] for r in req_lower if r not in found_lower
            ]
        else:
            to_copy = all_artifacts

        # Download the selected files
        for art in to_copy:
            # Sanitise filename — strip any path component to prevent traversal
            from pathlib import PurePosixPath
            safe_name = PurePosixPath(art["fileName"]).name
            if not safe_name or "/" in safe_name or "\\" in safe_name or ".." in safe_name:
                logger.warning("Skipping artifact with unsafe filename: %r", art["fileName"])
                continue
            file_url  = (
                f"{os.getenv('JENKINS_URL', 'http://localhost:8080')}"
                f"/job/{job_name}/{build_number}/artifact/{art['relativePath']}"
            )
            file_dest = dest / safe_name
            file_dest.write_bytes(_jenkins_get(file_url).content)
            staged.append({"name": safe_name, "path": str(file_dest), "download_url": file_url})
            logger.info("Artifact staged: %s", file_dest)

        if not_found:
            logger.warning("Requested artifacts not found in build: %s", not_found)

    except Exception as exc:
        logger.warning("Artifact copy incomplete (non-fatal): %s", exc)

    return {
        "dest":      dest,
        "staged":    staged,
        "not_found": not_found,
        "selective": selective,
    }


def _compose_artifact_summary(art_result: dict) -> str:
    """Build the artifact summary string for the build completion message."""
    dest = art_result["dest"]
    if art_result["selective"] and art_result["staged"]:
        file_lines = "\n".join(f"  • {f['name']}" for f in art_result["staged"])
        summary = f"Staged {len(art_result['staged'])} file(s) to `{dest}`:\n{file_lines}"
        if art_result["not_found"]:
            missing_names = ", ".join(f"`{f}`" for f in art_result["not_found"])
            summary += f"\n⚠️ Not found in build output: {missing_names}"
        return summary
    if art_result["staged"]:
        return f"Artifacts: `{dest}` ({len(art_result['staged'])} file(s))"
    return f"Artifacts folder: `{dest}`"


def _send_notifications(
    job_name: str,
    build_number: int,
    result: dict,
    build_url: str,
    art_result: dict,
    failure_cause: str,
    failure_suggestion: str,
    triggered_by: str,
    params: dict,
) -> list[str]:
    """
    Send GChat and email notifications. Returns a list of status strings
    to append to the build completion message.
    """
    from services import notifier

    email_body = (
        f"Job:       {job_name}\n"
        f"Build #:   {build_number}\n"
        f"Result:    {result['result']}\n"
        f"Duration:  {result['duration_str']}\n"
        f"URL:       {result['url']}\n"
        f"Artifacts: {art_result['dest']}\n"
    )
    if result["result"] != "SUCCESS" and failure_cause:
        email_body += f"\nFailure cause: {failure_cause}\nSuggestion: {failure_suggestion}\n"

    gchat_ok = False
    email_ok = False

    try:
        gchat_ok = notifier.send_gchat_build_result(
            job_name           = job_name,
            build_number       = build_number,
            result             = result["result"],
            duration_str       = result["duration_str"],
            build_url          = build_url,
            artifact_path      = str(art_result["dest"]),
            staged_files       = art_result.get("staged", []),
            not_found_files    = art_result.get("not_found", []),
            failure_cause      = failure_cause,
            failure_suggestion = failure_suggestion,
            triggered_by       = triggered_by,
            build_params       = params,
        )
    except Exception as exc:
        logger.warning("GChat notification error (non-fatal): %s",
                       re.sub(r'[\r\n\t]', '_', str(exc)))

    try:
        email_ok = notifier.send_email(
            subject = f"BuildBot: Build #{build_number} {result['result']}",
            body    = email_body,
        )
    except Exception as exc:
        logger.warning("Email notification error (non-fatal): %s", exc)

    notif_parts: list[str] = []
    if gchat_ok:
        notif_parts.append("📬 GChat notified")
    elif os.getenv("GCHAT_WEBHOOK", "").strip():
        notif_parts.append("⚠️ GChat failed — check app logs")
    if email_ok:
        notif_parts.append("📧 Email sent")
    elif os.getenv("NOTIFY_EMAIL_TO", "").strip():
        notif_parts.append("⚠️ Email failed — check app logs")
    return notif_parts


def _run_build(
    job_id: str,
    job_name: str,
    params: dict,
    requested_artifacts: list[str] | None = None,
    user_jenkins_auth=None,
    triggered_by: str = "unknown",
) -> None:
    from services.jenkins_client import (
        JenkinsBuildTimeoutError, JenkinsDownError, JenkinsError,
        JenkinsQueueTimeoutError, analyze_console_failure,
        poll_build, poll_queue, trigger_build,
    )

    try:
        _update_job(job_id, "queuing",  "Triggering Jenkins build…")
        # Use the triggering user's own Jenkins credentials so Jenkins enforces
        # their permissions natively. Falls back to service account when None.
        queue_url = trigger_build(job_name, params, user_auth=user_jenkins_auth)

        _update_job(job_id, "queued",   "Build queued — waiting for an executor…")
        build_number = poll_queue(queue_url)

        build_url = (
            f"{os.getenv('JENKINS_URL', 'http://localhost:8080')}"
            f"/job/{job_name}/{build_number}/"
        )
        _update_job(
            job_id, "building",
            f"Build #{build_number} started — building…",
            build_number=build_number, build_url=build_url,
        )

        result       = poll_build(job_name, build_number)
        console_tail = result.get("console_tail", [])
        art_result   = _copy_artifacts(job_name, build_number, requested_artifacts or [])

        # ── Compose artifact summary ──────────────────────────────────────────────
        dest = art_result["dest"]
        if art_result["selective"] and art_result["staged"]:
            file_lines = "\n".join(f"  • {f['name']}" for f in art_result["staged"])
            artifact_summary = f"Staged {len(art_result['staged'])} file(s) to `{dest}`:\n{file_lines}"
            if art_result["not_found"]:
                missing_names = ", ".join(f"`{f}`" for f in art_result["not_found"])
                artifact_summary += f"\n⚠️ Not found in build output: {missing_names}"
        elif art_result["staged"]:
            artifact_summary = f"Artifacts: `{dest}` ({len(art_result['staged'])} file(s))"
        else:
            artifact_summary = f"Artifacts folder: `{dest}`"

        # ── Compose final message ─────────────────────────────────────────────────
        is_success = result["result"] == "SUCCESS"
        icon       = "✅" if is_success else "❌"
        final_msg  = (
            f"{icon} Build #{build_number} **{result['result']}** "
            f"({result['duration_str']}). {artifact_summary}"
        )

        # ── Failure diagnosis (LLM-powered, regex fallback) ─────────────────────
        failure_cause      = ""
        failure_suggestion = ""
        if not is_success and console_tail:
            failure_cause, failure_suggestion = _diagnose_failure(console_tail, result["result"])

        # ── Notifications ─────────────────────────────────────────────────────────
        notif_parts = _send_notifications(
            job_name, build_number, result, build_url, art_result,
            failure_cause, failure_suggestion, triggered_by, params,
        )

        if notif_parts:
            final_msg += "  ·  " + "  ·  ".join(notif_parts)

        _update_job(
            job_id,
            status             = "success" if is_success else "failed",
            message            = final_msg,
            done               = True,
            build_number       = build_number,
            build_url          = build_url,
            artifact_path      = str(dest),
            staged_files       = art_result["staged"],
            not_found_files    = art_result["not_found"],
            selective_staging  = art_result["selective"],
            console_tail       = console_tail,
            result             = result["result"],
            failure_cause      = failure_cause,
            failure_suggestion = failure_suggestion,
        )

    except (JenkinsDownError, JenkinsQueueTimeoutError,
            JenkinsBuildTimeoutError, JenkinsError) as exc:
        _update_job(job_id, "error", f"❌ Jenkins error: {exc}", done=True)
    except Exception as exc:
        logger.exception("Unexpected error in build thread %s", job_id)
        _update_job(job_id, "error", f"❌ Unexpected error: {exc}", done=True)


# ─── Routes ───────────────────────────────────────────────────────────────────────

@app.route("/")
@require_auth
def index():
    return render_template("index.html", user=current_user())


@app.route("/chat", methods=["POST"])
@require_auth
@limiter.limit(_CHAT_LIMIT, error_message="Too many requests. Slow down.")
def chat():
    # Debug: log session state on every chat request
    logger.info("POST /chat — session keys: %s, auth_user: %s",
                list(session.keys()),
                re.sub(r'[\r\n\t]', '_', str(session.get("auth_user", ""))))
    sid      = _get_sid()
    user     = current_user()
    data     = request.get_json(force=True)
    if data is None:
        logger.warning("POST /chat — could not parse JSON body (empty or malformed request)")
        return jsonify({"reply": "⚠️ Request body was empty or could not be parsed. Please try again.", "param_card": None, "job_card": None}), 400
    msg_type = data.get("type", "message")
    chat_id  = data.get("chat_id", "default")
    sess_key = f"{sid}:{chat_id}"

    # ── Phase 1: natural-language message ──────────────────────────────────────
    if msg_type == "message":
        user_message = data.get("message", "").strip()[:2000]   # max 2000 chars
        if not user_message:
            return jsonify({"reply": "Please type a build request.", "param_card": None, "job_card": None})
        # Extract conversation context sent by the frontend (last 3 turns)
        raw_ctx = data.get("context", [])
        context = [
            {"role": c["role"], "content": str(c.get("content", ""))[:300]}
            for c in raw_ctx
            if isinstance(c, dict) and c.get("role") in ("user", "assistant") and c.get("content")
        ][-6:]   # cap at 6 messages (3 turns)
        return jsonify(_handle_message(sess_key, user_message, context=context))

    # ── Phase 1b: dev selected a job from the picker ────────────────────────────
    if msg_type == "job_select":
        job_name = data.get("job_name", "").strip()
        if not job_name:
            return jsonify({"reply": "No job name received.", "param_card": None, "job_card": None})
        # Authorization check
        if not can_trigger_job(job_name):
            return jsonify({
                "reply":      f"⛔ You don't have permission to trigger **`{job_name}`**. "
                              f"Contact your admin to request access.",
                "param_card": None,
                "job_card":   None,
            })
        return jsonify(_handle_job_select(sess_key, job_name))

    # ── Phase 2: param card submitted → trigger build ───────────────────────────
    if msg_type == "param_submit":
        params              = data.get("params", {})
        conv                = _conv_sessions.get(sess_key, {})
        job_name            = conv.get("job_name", "")
        requested_artifacts = conv.get("requested_artifacts", [])

        if not job_name:
            return jsonify({
                "reply": (
                    "⚠️ Session expired — the build parameters were lost "
                    "(the server likely reloaded). Please send your build request again."
                ),
                "param_card": None,
                "job_card":   None,
            })

        # Final authorization check before triggering
        if not can_trigger_job(job_name):
            user = current_user()
            logger.warning("Unauthorized build attempt by %r for job %r",
                           user["username"] if user else "unknown", job_name)
            return jsonify({
                "reply":      f"⛔ You don't have permission to trigger **`{job_name}`**.",
                "param_card": None,
                "job_card":   None,
            })

        # Validate params against known schema keys — prevent injection of extra fields
        known_schema = {p["name"] for p in conv.get("schema", [])}
        if known_schema:
            params = {k: v for k, v in params.items() if k in known_schema}

        job_id = str(uuid.uuid4())
        _build_jobs[job_id] = {
            "status":     "starting",
            "message":    "Starting build…",
            "done":       False,
            "owner":      current_user()["username"] if current_user() else "unknown",
            "created_at": time.time(),
        }

        # ── Extract Jenkins credentials BEFORE spawning thread ────────────────────
        # Flask session is not accessible in background threads — capture now.
        username   = user["username"] if user else "unknown"
        credential = get_user_jenkins_credential()   # None for local-mode users

        user_jenkins_auth = None
        if credential:
            from requests.auth import HTTPBasicAuth
            user_jenkins_auth = HTTPBasicAuth(username, credential)
            logger.info("Build will use personal Jenkins credentials for user %r", username)
        else:
            logger.info("Build will use service account credentials (user %r has no Jenkins token in session)",
                        re.sub(r'[\r\n\t]', '_', username))

        threading.Thread(
            target=_run_build,
            args=(job_id, job_name, params, requested_artifacts,
                  user_jenkins_auth, username),
            daemon=True,
        ).start()

        logger.info("Build triggered by user %r: job=%s job_id=%s",
                    re.sub(r'[\r\n\t]', '_', username),
                    re.sub(r'[\r\n\t]', '_', job_name),
                    job_id)

        return jsonify({
            "reply":        f"⏳ Triggering **`{job_name}`** — queuing now…",
            "param_card":   None,
            "job_card":     None,
            "build_job_id": job_id,
            "job_name":     job_name,
        })

    # ── Job browser sort ──────────────────────────────────────────────────────
    if msg_type == "job_browser_sort":
        sort_val = data.get("sort", "name")
        filters  = _browser_filters_from_session(sess_key)
        filters["sort"] = sort_val
        filters["page"] = 1
        return jsonify(_build_job_browser_response(sess_key, filters))

    # ── Job browser page ──────────────────────────────────────────────────────
    if msg_type == "job_browser_page":
        try:
            page_num = max(1, int(data.get("page", 1)))
        except (TypeError, ValueError):
            page_num = 1
        filters  = _browser_filters_from_session(sess_key)
        filters["page"] = page_num
        return jsonify(_build_job_browser_response(sess_key, filters))

    return jsonify({"reply": "Unknown message type.", "param_card": None, "job_card": None})


@app.route("/build-status/<job_id>")
def build_status(job_id: str):
    """
    Polled by the frontend every 3s for live build progress.
    No auth required — the job_id UUID is effectively a one-time secret.
    Ownership check still applies for non-anonymous users.
    """
    state = _build_jobs.get(job_id)
    if not state:
        return jsonify({"status": "unknown", "message": "Build job not found.", "done": True}), 404
    # Ownership check — only the triggering user or an admin can see this build
    user = current_user()
    if user and user.get("role") != "admin" and state.get("owner") and state.get("owner") != user["username"]:
        audit_logger.warning("UNAUTHORIZED_STATUS  user=%s  job_id=%s  owner=%s",
                             _sl(user["username"]), _sl(job_id), _sl(state.get("owner", "")))
        return jsonify({"status": "unknown", "message": "Not authorised.", "done": True}), 403
    return jsonify(state)


@app.route("/test-card")
@require_auth
def test_card():
    """Dev helper — hard-coded param card for UI testing without Jenkins."""
    sample_card = [
        {"name": "GITHUB_URL", "type": "StringParameterDefinition",
         "value": "https://github.com/acme/payments-api", "choices": [],
         "description": "Repository URL to build from", "source": "you provided"},
        {"name": "BRANCH", "type": "StringParameterDefinition",
         "value": "hotfix/PAY-4821", "choices": [],
         "description": "Git branch to build", "source": "you provided"},
        {"name": "RUN_TESTS", "type": "BooleanParameterDefinition",
         "value": True, "choices": [],
         "description": "Run unit tests after build", "source": "default"},
        {"name": "MODULES", "type": "ChoiceParameterDefinition",
         "value": ["Payments.Core", "Payments.Api"],
         "choices": ["Payments.Core", "Payments.Api", "Payments.Web", "ALL"],
         "description": "Modules to include", "source": "you provided"},
    ]
    return jsonify({"reply": "Ready to build. Review parameters below:",
                    "param_card": sample_card, "job_card": None})


@app.route("/test-job-picker")
@require_auth
def test_job_picker():
    """Dev helper — hard-coded job picker card for UI testing."""
    sample_jobs = [
        {"name": "hotfix-build",  "description": "Builds hotfix branches for Payments service", "status": "last build: SUCCESS"},
        {"name": "release-build", "description": "Creates a release package",                   "status": "last build: SUCCESS"},
        {"name": "nightly-build", "description": "Full nightly test suite",                     "status": "last build: FAILED"},
    ]
    return jsonify({"reply": "I found 3 jobs. Which one should I trigger?",
                    "param_card": None, "job_card": sample_jobs})


# ─── Service status endpoint ─────────────────────────────────────────────────────

@app.route("/debug-session")
def debug_session():
    """Temporary debug endpoint — shows session state without auth check."""
    import json as _json
    info = {
        "session_keys":  list(session.keys()),
        "auth_user":     session.get("auth_user"),
        "auth_mode":     session.get("auth_mode"),
        "has_last_active": "last_active" in session,
        "session_permanent": session.permanent,
        "cookie_name":   app.config.get("SESSION_COOKIE_NAME", "session"),
    }
    return jsonify(info)


@app.route("/status")
@require_auth
def service_status():
    """
    Returns live connectivity status for Jenkins and the LLM.
    Polled by the frontend every 30 seconds for the header status indicators.
    """
    import time as _time
    import requests as _req

    # Jenkins
    jenkins_ok = False
    try:
        from services import jenkins_client
        jenkins_ok = jenkins_client.check_jenkins_alive()
    except (OSError, Exception) as _je:
        logger.debug("Jenkins liveness check failed: %s", _je)

    # LLM — lightweight liveness check, no tokens consumed.
    # Uses /v1/models (standard OpenAI-compatible endpoint) rather than the
    # root URL which many API servers return 4xx on.
    # Respects LLM_VERIFY_SSL so self-signed / internal certs don't show false red.
    llm_ok = False
    try:
        _verify_ssl = os.getenv("LLM_VERIFY_SSL", "true").strip().lower() != "false"
        llm_base    = os.getenv("LLM_URL", "").split("/v1")[0]
        r = _req.get(
            f"{llm_base}/v1/models",
            timeout       = 8,
            allow_redirects = True,
            verify        = _verify_ssl,
        )
        llm_ok = r.status_code < 500
    except (OSError, Exception) as _le:
        logger.debug("LLM liveness check failed: %s", _le)

    return jsonify({
        "jenkins": "ok" if jenkins_ok else "error",
        "llm":     "ok" if llm_ok     else "error",
        "ts":      _time.time(),
    })


# ─── Notification test endpoint ───────────────────────────────────────────────────

@app.route("/test-notify", methods=["GET", "POST"])
@require_auth
def test_notify():
    """
    Send a test notification through every configured channel and report results.

    GET  → returns the current notification config (no messages sent).
    POST → sends a test message to each enabled channel and returns a status dict.

    Admin-only — returns 403 for non-admin users.

    Response shape (POST):
    {
        "gchat": {
            "enabled": true,
            "ok":      true,
            "detail":  "Sent to https://chat.google…"
        },
        "email": {
            "enabled": true,
            "ok":      false,
            "detail":  "ConnectionRefusedError — is MailHog running on localhost:1025?"
        }
    }
    """
    user = current_user()
    if not user or user.get("role") != "admin":
        return jsonify({"error": "Admin access required."}), 403

    gchat_webhook  = os.getenv("GCHAT_WEBHOOK",        "").strip()
    smtp_host      = os.getenv("SMTP_HOST",            "localhost")
    smtp_port      = int(os.getenv("SMTP_PORT",        "1025"))
    email_to       = os.getenv("NOTIFY_EMAIL_TO",      "").strip()
    email_from     = os.getenv("NOTIFY_EMAIL_FROM",    "buildbot@local")

    # ── GET — return config without sending ──────────────────────────────────────
    if request.method == "GET":
        return jsonify({
            "gchat": {
                "enabled": bool(gchat_webhook),
                "webhook": (gchat_webhook[:40] + "…") if len(gchat_webhook) > 40 else gchat_webhook,
            },
            "email": {
                "enabled":   bool(email_to),
                "smtp_host": smtp_host,
                "smtp_port": smtp_port,
                "from":      email_from,
                "to":        email_to,
            },
        })

    # ── POST — send test messages ────────────────────────────────────────────────
    from services import notifier
    import time as _t

    username  = user.get("username", "admin")
    timestamp = __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    results: dict = {}

    # Google Chat test
    gchat_result: dict = {"enabled": bool(gchat_webhook)}
    if gchat_webhook:
        try:
            ok = notifier.send_gchat_build_result(
                job_name           = "test-job",
                build_number       = 0,
                result             = "SUCCESS",
                duration_str       = "0s",
                build_url          = "",
                artifact_path      = "",
                staged_files       = [],
                not_found_files    = [],
                failure_cause      = "",
                failure_suggestion = "",
                triggered_by       = username,
                build_params       = {"BRANCH": "test/notification-check"},
            )
            gchat_result["ok"]     = ok
            gchat_result["detail"] = (
                f"Test card sent successfully at {timestamp}."
                if ok else
                "Delivery failed — HTTP error logged in the app console. "
                "Check that GCHAT_WEBHOOK is a valid incoming-webhook URL."
            )
        except Exception as exc:
            gchat_result["ok"]     = False
            gchat_result["detail"] = str(exc)
    else:
        gchat_result["ok"]     = False
        gchat_result["detail"] = "GCHAT_WEBHOOK is not set in .env."
    results["gchat"] = gchat_result

    # Email test
    email_result: dict = {"enabled": bool(email_to)}
    if email_to:
        try:
            ok = notifier.send_email(
                subject = f"[BuildBot] Test notification — {timestamp}",
                body    = (
                    f"This is a test notification from BuildBot.\n\n"
                    f"Sent by:   {username}\n"
                    f"Time:      {timestamp}\n"
                    f"SMTP:      {smtp_host}:{smtp_port}\n"
                    f"From:      {email_from}\n"
                    f"To:        {email_to}\n\n"
                    f"If you received this, email notifications are working correctly."
                ),
            )
            email_result["ok"]     = ok
            email_result["detail"] = (
                f"Email sent to {email_to} at {timestamp}."
                if ok else
                f"Delivery failed — is MailHog (or your SMTP server) "
                f"running on {smtp_host}:{smtp_port}? "
                f"Check the app console for the exact error."
            )
        except Exception as exc:
            email_result["ok"]     = False
            email_result["detail"] = str(exc)
    else:
        email_result["ok"]     = False
        email_result["detail"] = "NOTIFY_EMAIL_TO is not set in .env."
    results["email"] = email_result

    audit_logger.info(
        "TEST_NOTIFY  user=%s  gchat=%s  email=%s",
        username,
        "ok" if results["gchat"].get("ok") else "fail",
        "ok" if results["email"].get("ok") else "fail",
    )
    return jsonify(results)


# ─── Chat session management ─────────────────────────────────────────────────────

@app.route("/sessions", methods=["GET"])
@require_auth
def list_chat_sessions():
    """Return all chat sessions for the current user (for sidebar rendering)."""
    import time as _time
    sid        = _get_sid()
    user_chats = {k: v for k, v in _user_chats.get(sid, {}).items() if k != "_created_at"}
    sessions   = sorted(
        [{"id": cid, "title": info["title"]} for cid, info in user_chats.items()],
        key=lambda x: user_chats.get(x["id"], {}).get("created_at", 0),
        reverse=True,
    )
    return jsonify({"sessions": sessions})


@app.route("/sessions", methods=["POST"])
@require_auth
def new_chat_session():
    """Create a new chat session and return its ID."""
    import time as _time
    sid     = _get_sid()
    chat_id = str(uuid.uuid4())
    user_entry = _user_chats.setdefault(sid, {"_created_at": _time.time()})
    user_entry[chat_id] = {
        "title":      "New chat",
        "created_at": _time.time(),
    }
    _save_user_chats()
    return jsonify({"chat_id": chat_id})


@app.route("/sessions/<chat_id>", methods=["PATCH"])
@require_auth
def rename_chat_session(chat_id: str):
    """Update the title of a chat session (called after the first message)."""
    sid  = _get_sid()
    data = request.get_json(force=True)
    if sid in _user_chats and chat_id in _user_chats[sid]:
        _user_chats[sid][chat_id]["title"] = data.get("title", "Chat")[:60]
    _save_user_chats()
    return jsonify({"ok": True})


@app.route("/sessions/<chat_id>", methods=["DELETE"])
@require_auth
def delete_chat_session(chat_id: str):
    """Delete a chat session and its associated conversation state."""
    sid = _get_sid()
    if sid in _user_chats:
        _user_chats[sid].pop(chat_id, None)
    # Clean up the conversation session for this chat
    _conv_sessions.pop(f"{sid}:{chat_id}", None)
    _save_user_chats()
    _save_conv_sessions()
    return jsonify({"ok": True})


# ─── Auth routes (only active when AUTH_ENABLED=true) ────────────────────────────

@app.route("/login", methods=["GET", "POST"])
@limiter.limit(_LOGIN_LIMIT, error_message="Too many login attempts. Please wait a minute.")
def login_page():
    from services.auth import auth_enabled, auth_mode as _auth_mode, is_logged_in
    logger.info("login_page — method: %s, session keys: %s", request.method, list(session.keys()))

    # Already logged in → go home
    if is_logged_in():
        return redirect(url_for("index"))

    # Auth disabled → go home directly
    if not auth_enabled():
        return redirect(url_for("index"))

    error    = None
    username = ""
    mode     = _auth_mode()
    next_url = request.args.get("next", "")   # default from query string (GET)

    if request.method == "POST":
        username   = request.form.get("username", "").strip()
        credential = request.form.get("password", "")
        # Keep next_url from form (overrides query string on POST)
        next_url   = request.form.get("next", "") or next_url

        if not username or not credential:
            error = "Please enter your username and password / API token."
        else:
            success, err = login_user(username, credential)
            if success:
                audit_logger.info("LOGIN_SUCCESS  user=%s  ip=%s  mode=%s",
                                  _sl(username), _sl(request.remote_addr), _sl(mode))
                return redirect(_safe_redirect_url(next_url))
            audit_logger.warning("LOGIN_FAILED  user=%s  ip=%s  reason=%s",
                                 _sl(username), _sl(request.remote_addr), _sl(err))
            error = err or "Login failed. Check your credentials and try again."
    return render_template("login.html", error=error, username=username,
                           next=next_url, auth_mode=mode)


@app.route("/logout")
def logout():
    logout_user()
    return redirect(url_for("login_page"))


@app.route("/me")
@require_auth
def me():
    """Return current user info as JSON (used by the frontend)."""
    user = current_user()
    if not user:
        return jsonify({"error": "not authenticated"}), 401
    return jsonify({
        "username":     user["username"],
        "display_name": user["display_name"],
        "role":         user["role"],
        "allowed_jobs": user["allowed_jobs"],
    })


# ─── Admin user management API ───────────────────────────────────────────────────

@app.route("/admin/users")
@require_admin
def admin_users_page():
    """Render the admin user management panel."""
    from services.auth import load_users
    all_users = load_users().get("users", {})
    return render_template("admin_users.html", user=current_user(), users=all_users)


@app.route("/admin/api/users", methods=["GET"])
@require_admin
def admin_api_list_users():
    """Return all users as JSON (passwords excluded)."""
    from services.auth import load_users
    raw = load_users().get("users", {})
    safe = {
        uname: {k: v for k, v in data.items() if k != "password_hash"}
        for uname, data in raw.items()
    }
    return jsonify(safe)


@app.route("/admin/api/users", methods=["POST"])
@require_admin
def admin_api_create_user():
    """Create a new user. Body: {username, display_name, role, allowed_jobs, password}"""
    from services.auth import hash_password, load_users, save_users, validate_password
    data     = request.get_json(force=True)
    username = data.get("username", "").strip().lower()
    password = data.get("password", "")

    if not username:
        return jsonify({"error": "username is required"}), 400
    if not username.isidentifier():
        return jsonify({"error": "username must contain only letters, digits, and underscores"}), 400

    db = load_users()
    if username in db.get("users", {}):
        return jsonify({"error": f"User '{username}' already exists"}), 409

    errors = validate_password(password)
    if errors:
        return jsonify({"error": "Password policy violation", "details": errors}), 422

    role         = data.get("role", "developer")
    allowed_jobs = data.get("allowed_jobs", [])
    if role == "admin":
        allowed_jobs = "*"

    db.setdefault("users", {})[username] = {
        "display_name":  data.get("display_name", username).strip() or username,
        "password_hash": hash_password(password),
        "role":          role,
        "allowed_jobs":  allowed_jobs,
        "active":        True,
    }
    save_users(db)
    logger.info("Admin %r created user %r (role=%s)", current_user()["username"], username, _sl(role))
    return jsonify({"ok": True, "username": username}), 201


@app.route("/admin/api/users/<username>", methods=["PATCH"])
@require_admin
def admin_api_update_user(username: str):
    """Update display_name, role, allowed_jobs, or active flag."""
    from services.auth import load_users, save_users
    db    = load_users()
    users = db.get("users", {})
    if username not in users:
        return jsonify({"error": f"User '{username}' not found"}), 404

    data = request.get_json(force=True)

    # Prevent removing the last admin
    if data.get("role") and data["role"] != "admin" and users[username].get("role") == "admin":
        other_admins = [
            u for u, v in users.items()
            if u != username and v.get("role") == "admin" and v.get("active", True)
        ]
        if not other_admins:
            return jsonify({"error": "Cannot demote the last active admin"}), 409

    if data.get("active") is False and users[username].get("role") == "admin":
        other_admins = [
            u for u, v in users.items()
            if u != username and v.get("role") == "admin" and v.get("active", True)
        ]
        if not other_admins:
            return jsonify({"error": "Cannot disable the last active admin"}), 409

    for field in ("display_name", "role", "allowed_jobs", "active"):
        if field in data:
            users[username][field] = data[field]

    # Auto-set allowed_jobs when promoting to admin
    if data.get("role") == "admin":
        users[username]["allowed_jobs"] = "*"

    save_users(db)
    logger.info("Admin %r updated user %r: %s", current_user()["username"], username, list(data.keys()))
    return jsonify({"ok": True})


@app.route("/admin/api/users/<username>/password", methods=["PUT"])
@require_admin
def admin_api_change_password(username: str):
    """Change a user's password."""
    from services.auth import hash_password, load_users, save_users, validate_password
    db    = load_users()
    users = db.get("users", {})
    if username not in users:
        return jsonify({"error": f"User '{username}' not found"}), 404

    data     = request.get_json(force=True)
    password = data.get("password", "")
    errors   = validate_password(password)
    if errors:
        return jsonify({"error": "Password policy violation", "details": errors}), 422

    users[username]["password_hash"] = hash_password(password)
    save_users(db)
    logger.info("Admin %r changed password for user %r", current_user()["username"], username)
    return jsonify({"ok": True})


@app.route("/admin/api/users/<username>", methods=["DELETE"])
@require_admin
def admin_api_delete_user(username: str):
    """Permanently delete a user."""
    from services.auth import load_users, save_users
    db    = load_users()
    users = db.get("users", {})
    if username not in users:
        return jsonify({"error": f"User '{username}' not found"}), 404

    if users[username].get("role") == "admin":
        other_admins = [
            u for u, v in users.items()
            if u != username and v.get("role") == "admin" and v.get("active", True)
        ]
        if not other_admins:
            return jsonify({"error": "Cannot delete the last active admin"}), 409

    del users[username]
    save_users(db)
    logger.info("Admin %r deleted user %r", current_user()["username"], username)
    return jsonify({"ok": True})


@app.route("/admin/api/jenkins-jobs")
@require_admin
def admin_api_jenkins_jobs():
    """Return available Jenkins job names for the admin panel job picker."""
    try:
        from services import jenkins_client
        jobs = jenkins_client.list_jobs()
        return jsonify({"jobs": [j["name"] for j in jobs], "ok": True})
    except Exception as exc:
        logger.warning("Could not fetch Jenkins jobs for admin panel: %s", exc)
        return jsonify({"jobs": [], "ok": False, "error": str(exc)})


# ─── Job browser API routes ──────────────────────────────────────────────────────

@app.route("/api/jobs")
@require_auth
@limiter.limit("60 per minute", error_message="Too many job list requests.")
def api_jobs():
    """Filtered, sorted, paginated job list for the job browser widget."""
    from services import jenkins_client

    def _int(val, default, lo, hi):
        try:
            return max(lo, min(hi, int(val)))
        except (TypeError, ValueError):
            return default

    try:
        all_jobs = jenkins_client.list_jobs()
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502

    result = jenkins_client.filter_and_page_jobs(
        all_jobs,
        q              = request.args.get("q", "").strip()[:200],         # cap search length
        status         = request.args.get("status", "").strip().upper()[:20],
        hotfix         = request.args.get("hotfix", "").lower() == "true",
        repo           = request.args.get("repo", "").strip()[:200],
        branch         = request.args.get("branch", "").strip()[:200],
        repo_param_key = request.args.get("repo_param_key", "").strip()[:50],
        page           = _int(request.args.get("page",     1),  1,  1, 9999),
        per_page       = _int(request.args.get("per_page", 20), 20, 5,   50),
        sort           = request.args.get("sort", "name").strip()[:20],
    )
    return jsonify(result)


@app.route("/api/jobs/<path:job_name>/detail")
@require_auth
@limiter.limit("30 per minute", error_message="Too many job detail requests.")
def api_job_detail(job_name: str):
    """Return last 5 builds, artifacts, and last failure info for a job."""
    from services import jenkins_client
    try:
        builds = jenkins_client.list_build_history(job_name, 5)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502

    artifacts = []
    try:
        art_data  = jenkins_client.list_build_artifacts(job_name)
        artifacts = art_data.get("artifacts", [])
    except (OSError, ValueError, KeyError) as exc:
        logger.debug("api_job_detail: could not fetch artifacts for %r: %s", job_name, exc)

    last_failure = None
    failed_builds = [b for b in builds if b["result"] in ("FAILURE", "UNSTABLE")]
    if failed_builds:
        fb        = failed_builds[0]
        log_lines = []
        try:
            log_data  = jenkins_client.get_console_log_tail(job_name, fb["number"], 20)
            log_lines = log_data.get("lines", [])
        except (OSError, ValueError, KeyError) as exc:
            logger.debug("api_job_detail: could not fetch log for %r build %s: %s",
                         job_name, fb["number"], exc)
        last_failure = {
            "build_number": fb["number"],
            "result":       fb["result"],
            "log_lines":    log_lines,
        }

    return jsonify({
        "job_name":     job_name,
        "builds":       builds,
        "artifacts":    artifacts,
        "last_failure": last_failure,
    })


# ─── Entry point — MUST be last in this file ─────────────────────────────────────
if __name__ == "__main__":
    debug_mode = os.getenv("FLASK_DEBUG", "0") == "1"
    app.run(debug=debug_mode, port=5000)

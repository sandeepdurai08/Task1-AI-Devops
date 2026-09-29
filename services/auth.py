"""
services/auth.py
────────────────
Modular authentication + authorisation layer for BuildBot.

AUTH_MODE controls how users authenticate:

  jenkins  (recommended)
    • Users log in with their Jenkins username + password or API token.
    • BuildBot verifies credentials against Jenkins /api/json.
    • Job permissions are read from Jenkins automatically — no manual config.
    • If AUTH_FALLBACK=true, local users.json admin accounts always work as
      emergency access even when Jenkins is unreachable.

  local
    • Users are managed entirely in config/users.json.
    • Passwords hashed with werkzeug (scrypt).
    • Original behaviour — no Jenkins dependency for login.

Both modes respect AUTH_ENABLED=false (no login at all).

Session keys set on login:
    auth_mode          "jenkins" | "local"
    auth_user          username string (always set)

  Jenkins-mode only:
    jenkins_credential password or API token (used for build triggers)
    jenkins_allowed    list of job names the user can see in Jenkins

Public API
──────────
    auth_mode()                  → "jenkins" | "local"
    auth_enabled()               → bool
    login_user(u, credential)    → bool   (credential = password or API token)
    logout_user()
    current_user()               → dict | None
    is_logged_in()               → bool
    can_trigger_job(job_name)    → bool
    get_user_jenkins_credential()→ str | None   (for build thread)
    require_auth                 — Flask route decorator
    require_admin                — Flask route decorator (admin role only)
    validate_password(pw)        → [errors]
    hash_password(pw)            → str
    load_users()                 → dict
    save_users(data)
"""

import json
import logging
import logging.handlers
import os
import re
import time
from functools import wraps
from pathlib import Path

import requests as _requests
from flask import redirect, request, session, url_for
from requests.auth import HTTPBasicAuth
from werkzeug.security import check_password_hash, generate_password_hash

logger = logging.getLogger(__name__)

# ─── Config helpers ──────────────────────────────────────────────────────────────

_USERS_FILE = Path(__file__).parent.parent / "config" / "users.json"

_AUTH_ENABLED_CACHE: bool | None = None


def auth_enabled() -> bool:
    global _AUTH_ENABLED_CACHE
    if _AUTH_ENABLED_CACHE is None:
        _AUTH_ENABLED_CACHE = os.getenv("AUTH_ENABLED", "true").strip().lower() in ("true", "1", "yes")
    return _AUTH_ENABLED_CACHE


def auth_mode() -> str:
    """Return 'jenkins' or 'local'. Defaults to 'jenkins'."""
    return os.getenv("AUTH_MODE", "jenkins").strip().lower()


def auth_fallback_enabled() -> bool:
    """When True, local users.json admin accounts work even in jenkins mode."""
    return os.getenv("AUTH_FALLBACK", "true").strip().lower() in ("true", "1", "yes")


def _jenkins_base_url() -> str:
    return os.getenv("JENKINS_URL", "http://localhost:8080").rstrip("/")


# ─── User database I/O (local mode) ─────────────────────────────────────────────

_users_cache: dict | None = None
_users_mtime: float = 0.0


def load_users() -> dict:
    """Load users.json with mtime-based cache."""
    global _users_cache, _users_mtime
    try:
        mtime = _USERS_FILE.stat().st_mtime
        if _users_cache is not None and mtime == _users_mtime:
            return _users_cache
        with open(_USERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        _users_cache = data
        _users_mtime = mtime
        return data
    except FileNotFoundError:
        logger.warning("users.json not found at %s", _USERS_FILE)
        return {"users": {}}
    except json.JSONDecodeError as exc:
        logger.error("users.json malformed: %s", exc)
        return {"users": {}}


def save_users(data: dict) -> None:
    """Write users.json atomically and invalidate cache."""
    global _users_cache, _users_mtime
    _USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _USERS_FILE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    tmp.replace(_USERS_FILE)
    _users_cache = None
    _users_mtime = 0.0
    logger.info("users.json saved (%d users)", len(data.get("users", {})))


def _get_local_user(username: str) -> dict | None:
    return load_users().get("users", {}).get(username)


# ─── Password policy ─────────────────────────────────────────────────────────────

_SPECIAL_RE = re.compile(r"[!@#$%^&*\-_+=?.,:;]")
_UPPER_RE   = re.compile(r"[A-Z]")
_LOWER_RE   = re.compile(r"[a-z]")
_DIGIT_RE   = re.compile(r"\d")


def validate_password(password: str) -> list[str]:
    errors = []
    if len(password) < 8:
        errors.append("Password must be at least 8 characters.")
    if not _UPPER_RE.search(password):
        errors.append("Must include an uppercase letter (A–Z).")
    if not _LOWER_RE.search(password):
        errors.append("Must include a lowercase letter (a–z).")
    if not _DIGIT_RE.search(password):
        errors.append("Must include a digit (0–9).")
    if not _SPECIAL_RE.search(password):
        errors.append("Must include a special character (!@#$%^&*-_+=?.,:;).")
    return errors


def hash_password(password: str) -> str:
    return generate_password_hash(password)


# ─── Jenkins authentication ───────────────────────────────────────────────────────

def verify_jenkins_credentials(username: str, credential: str) -> bool:
    """
    Verify a Jenkins username + password-or-API-token by calling Jenkins /api/json.
    Returns True if Jenkins returns HTTP 200, False otherwise.
    Never raises.
    """
    if not username or not credential:
        return False
    try:
        resp = _requests.get(
            f"{_jenkins_base_url()}/api/json",
            auth=HTTPBasicAuth(username, credential),
            timeout=6,
        )
        if resp.status_code == 200:
            logger.info("Jenkins auth verified for user %r", username)
            return True
        logger.warning("Jenkins auth failed for %r: HTTP %d", username, resp.status_code)
        return False
    except Exception as exc:
        logger.warning("Jenkins unreachable during auth check: %s", exc)
        return False


def get_jenkins_jobs_for_user(username: str, credential: str) -> list[str]:
    """
    Fetch the list of Jenkins job names visible to this user (with their credentials).
    Visible jobs = jobs the user has at least READ access to in Jenkins.
    Build permission is enforced by Jenkins at trigger time (returns 403 if denied).

    Returns an empty list on any error (non-fatal).
    """
    try:
        resp = _requests.get(
            f"{_jenkins_base_url()}/api/json",
            auth=HTTPBasicAuth(username, credential),
            params={"tree": "jobs[name]"},
            timeout=6,
        )
        if resp.status_code != 200:
            return []
        jobs = [j["name"] for j in resp.json().get("jobs", []) if j.get("name")]

        # Apply JENKINS_JOBS allowlist if configured (extra security layer)
        allowlist_raw = os.getenv("JENKINS_JOBS", "").strip()
        if allowlist_raw:
            allowlist = {j.strip() for j in allowlist_raw.split(",") if j.strip()}
            jobs = [j for j in jobs if j in allowlist]

        logger.info("Jenkins user %r has access to %d job(s)", username, len(jobs))
        return jobs
    except Exception as exc:
        logger.warning("Could not fetch Jenkins jobs for %r: %s", username, exc)
        return []


def _client_ip() -> str:
    """Return the client IP for audit logging (respects X-Forwarded-For if set)."""
    try:
        from flask import request as _req
        xff = _req.headers.get("X-Forwarded-For", "")
        return xff.split(",")[0].strip() if xff else (_req.remote_addr or "unknown")
    except Exception:
        return "unknown"


# ─── Login / Logout ───────────────────────────────────────────────────────────────

def login_user(username: str, credential: str) -> tuple[bool, str]:
    """
    Authenticate a user.  credential = password or Jenkins API token.

    Decision tree:
      1. If AUTH_MODE=jenkins (or fallback enabled + user is in users.json as admin):
         a. If user is in users.json and active → try local password check first
            (covers the emergency admin account even when Jenkins is down)
         b. Otherwise → verify against Jenkins
      2. If AUTH_MODE=local:
         → verify against users.json only

    Returns (success: bool, error_message: str).
    Sets the Flask session on success.
    """
    mode     = auth_mode()
    fallback = auth_fallback_enabled()

    # ── Emergency fallback: local admin in users.json ─────────────────────────
    local_user = _get_local_user(username)
    if local_user and local_user.get("active", True):
        if fallback or mode == "local":
            if check_password_hash(local_user.get("password_hash", ""), credential):
                _set_local_session(username, local_user)
                logger.info("User %r logged in via local (IP: %s)", username, _client_ip())
                return True, ""
            elif mode == "local":
                logger.warning("Failed local login for user %r (IP: %s)", username, _client_ip())
                return False, "Invalid username or password."
            # else: password didn't match local hash → fall through to Jenkins check

    # ── Jenkins authentication ────────────────────────────────────────────────
    if mode == "jenkins":
        if not verify_jenkins_credentials(username, credential):
            logger.warning("Failed login — Jenkins rejected credentials for user %r (IP: %s)",
                           username, _client_ip())
            return False, (
                "Jenkins authentication failed. "
                "Check your username and password / API token."
            )
        # Credentials verified — fetch permitted jobs
        allowed_jobs = get_jenkins_jobs_for_user(username, credential)
        _set_jenkins_session(username, credential, allowed_jobs)
        logger.info("User %r logged in via Jenkins (%d jobs)", username, len(allowed_jobs))
        return True, ""

    return False, "Invalid username or password."


def _set_local_session(username: str, user_data: dict) -> None:
    # Session fixation fix — always start a fresh session on login
    session.clear()
    session["auth_user"]    = username
    session["auth_mode"]    = "local"
    session["last_active"]  = time.time()
    session.permanent       = True


def _set_jenkins_session(username: str, credential: str, allowed_jobs: list[str]) -> None:
    # Session fixation fix — always start a fresh session on login
    session.clear()
    session["auth_user"]          = username
    session["auth_mode"]          = "jenkins"
    session["jenkins_credential"] = credential
    session["jenkins_allowed"]    = allowed_jobs
    session["last_active"]        = time.time()
    session.permanent             = True


def logout_user() -> None:
    username = session.get("auth_user")
    session.clear()
    if username:
        logger.info("User %r logged out.", username)


# ─── Current user ─────────────────────────────────────────────────────────────────

def current_user() -> dict | None:
    """
    Return the current user as a normalised dict, or None if not logged in.

    Dict always contains:
        username, display_name, role, allowed_jobs, active, auth_mode
    """
    if not auth_enabled():
        return {
            "username":     "system",
            "display_name": "System (auth disabled)",
            "role":         "admin",
            "allowed_jobs": "*",
            "active":       True,
            "auth_mode":    "local",
        }

    username = session.get("auth_user")
    if not username:
        return None

    mode = session.get("auth_mode", "local")

    if mode == "jenkins":
        # User is authenticated via Jenkins — build their profile from session
        local = _get_local_user(username) or {}

        # Honour the active flag from users.json even for Jenkins-mode users
        if local and not local.get("active", True):
            session.clear()
            return None

        allowed_jobs = session.get("jenkins_allowed", [])
        role         = local.get("role", "developer")
        display      = local.get("display_name") or username

        return {
            "username":     username,
            "display_name": display,
            "role":         role,
            "allowed_jobs": allowed_jobs,
            "active":       local.get("active", True),
            "auth_mode":    "jenkins",
        }

    else:
        # Local mode — read from users.json
        user = _get_local_user(username)
        if not user or not user.get("active", True):
            session.clear()
            return None
        return {"username": username, "auth_mode": "local", **user}


def is_logged_in() -> bool:
    return current_user() is not None


def get_user_jenkins_credential() -> str | None:
    """
    Return the Jenkins credential (password or API token) stored in the session.
    Used by _run_build() to trigger builds as the authenticated user.
    Returns None for local-mode users (falls back to service account).
    """
    if session.get("auth_mode") == "jenkins":
        return session.get("jenkins_credential")
    return None


# ─── Authorisation ────────────────────────────────────────────────────────────────

def can_trigger_job(job_name: str) -> bool:
    """
    Return True if the current user is permitted to trigger job_name.

    Jenkins mode:
      - allowed_jobs list comes from Jenkins (jobs user can see)
      - admin role override from users.json still applies
    Local mode:
      - allowed_jobs from users.json
    """
    user = current_user()
    if not user:
        return False
    if user["role"] == "admin" or user["allowed_jobs"] == "*":
        return True
    if user["role"] == "readonly":
        return False
    return job_name in (user.get("allowed_jobs") or [])


def can_view_builds() -> bool:
    return current_user() is not None


# ─── Idle timeout ────────────────────────────────────────────────────────────────

def _IDLE_TIMEOUT_S() -> int:
    """Idle session timeout in seconds. Configurable via SESSION_IDLE_TIMEOUT_MIN (.env)."""
    return int(os.getenv("SESSION_IDLE_TIMEOUT_MIN", "480")) * 60


def _is_api_request() -> bool:
    """
    Return True if this is a JSON API call (not a browser page navigation).
    API routes should receive a 401 JSON response on auth failure, not an HTML redirect,
    so the frontend can distinguish genuine session expiry from server restarts.
    """
    _API_PREFIXES = (
        "/chat", "/sessions", "/api/", "/build-status/",
        "/admin/api/", "/me", "/status", "/debug-session",
    )
    return request.is_json or request.path.startswith(_API_PREFIXES)


def _check_and_refresh_idle() -> None | object:
    """
    Check if the session has been idle too long.
    - API requests  → returns a 401 JSON {"error": "auth_required"} response.
    - Page requests → returns an HTML redirect to /login.
    - Not expired   → updates last_active and returns None (caller continues).
    """
    last = session.get("last_active")
    now  = time.time()

    if last is None:
        # Session predates the idle-timeout feature — seed timestamp, don't expire
        session["last_active"] = now
        return None

    if now - last > _IDLE_TIMEOUT_S():
        username = session.get("auth_user", "unknown")
        logger.info("Session expired (idle timeout) for user %r", username)
        session.clear()
        if _is_api_request():
            from flask import jsonify as _j
            return _j({"error": "auth_required", "redirect": "/login"}), 401
        return redirect(url_for("login_page"))

    session["last_active"] = now
    return None


# ─── Flask decorators ─────────────────────────────────────────────────────────────

def require_auth(fn):
    """
    Protect a route: unauthenticated or timed-out requests are rejected.
    - API/JSON routes receive a 401 JSON response (so the frontend can tell the
      difference between a genuine auth failure and a server restart).
    - Browser page routes receive an HTML redirect to /login.
    No-op when AUTH_ENABLED=false.
    """
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not auth_enabled():
            return fn(*args, **kwargs)
        if not is_logged_in():
            if _is_api_request():
                from flask import jsonify as _j
                return _j({"error": "auth_required", "redirect": "/login"}), 401
            return redirect(url_for("login_page", next=request.url))
        result = _check_and_refresh_idle()
        if result is not None:
            return result
        return fn(*args, **kwargs)
    return wrapper


def require_admin(fn):
    """
    Require role=admin.
    - /admin/api/* routes → 403 JSON on insufficient role.
    - API/JSON routes     → 401 JSON when not authenticated.
    - Browser pages       → redirect to /login or /index.
    """
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not auth_enabled():
            return fn(*args, **kwargs)
        if not is_logged_in():
            if _is_api_request():
                from flask import jsonify as _j
                return _j({"error": "auth_required", "redirect": "/login"}), 401
            return redirect(url_for("login_page", next=request.url))
        result = _check_and_refresh_idle()
        if result is not None:
            return result
        user = current_user()
        if not user or user.get("role") != "admin":
            if request.path.startswith("/admin/api/"):
                from flask import jsonify as _j
                return _j({"error": "Admin access required."}), 403
            return redirect(url_for("index"))
        return fn(*args, **kwargs)
    return wrapper

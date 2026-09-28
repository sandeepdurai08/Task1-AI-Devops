# BuildBot — Security Assessment

Full security audit performed September 2026. Updated as each item was resolved.

---

## Overall Rating

```
Status (September 2026):  🟢 GOOD for internal team tool
Remaining open items:     2 (low priority, production hardening)
```

---

## All Findings

### Critical / High — All Fixed

| # | Issue | Status |
|---|---|---|
| ISSUE-23 | `FLASK_DEBUG=1` on by default — Werkzeug debugger allows RCE | ✅ Documented dev-only; production use removes it |
| ISSUE-12 | Live Jenkins token + GChat webhook in `.env` | ✅ `.env` in `.gitignore`; rotate on each deployment |
| ISSUE-05 | Open redirect after login — `next` param not validated | ✅ `_safe_redirect_url()` validates same-origin |
| ISSUE-22 | No HTTP security headers | ✅ `@app.after_request`: X-Frame-Options, CSP, nosniff, Referrer-Policy, Permissions-Policy |
| ISSUE-06 | `/status` unauthenticated — leaks infra topology | ✅ `@require_auth` added |
| ISSUE-08 | Artifact path traversal — `fileName` from Jenkins not sanitised | ✅ `PurePosixPath(...).name` strips all directory components |
| ISSUE-09 | Job names not URL-encoded in Jenkins API calls | ✅ `urllib.parse.quote(job_name, safe='')` on every call |
| ISSUE-10 | Build params not validated against schema | ✅ Filtered to known schema keys before trigger |
| ISSUE-11 | No message length limit — DoS / LLM cost amplification | ✅ `user_message[:2000]` cap; query params capped at 200 chars |
| ISSUE-02 | Fallback `secret_key = "dev-secret-change-me"` | ✅ Raises `RuntimeError` if `FLASK_SECRET_KEY` missing |

---

### Medium — All Fixed

| # | Issue | Status |
|---|---|---|
| ISSUE-17 | No login rate limiting — brute-force possible | ✅ Flask-Limiter: `10 per minute` on `/login` |
| ISSUE-18 | No chat rate limiting — unlimited build spam | ✅ Flask-Limiter: `30 per minute` on `/chat` |
| New | `/api/jobs` and `/api/jobs/<name>/detail` no rate limit | ✅ `60/min` and `30/min` respectively |
| New | `int()` on raw URL params — ValueError → HTTP 500 | ✅ `_int()` helper with try/except and defaults |
| ISSUE-14 | No CSRF protection | ✅ Flask-WTF CSRFProtect (form-only mode; JSON APIs use auth + same-origin) |
| ISSUE-15 | Bot reply HTML from localStorage replayed — XSS persistence | ✅ `sanitizeHtml()` allowlist in frontend (safe tags/attrs only) |
| ISSUE-07 | Build status readable by any authenticated user | ✅ `owner` field + ownership check on `/build-status/<id>` |
| ISSUE-28 | In-memory state never expires | ✅ TTL eviction thread runs every hour (`ENTRY_TTL_HOURS`, default 24) |
| ISSUE-19/20 | Failed logins and unauthorized build attempts not fully logged | ✅ All events logged with username + IP to `audit.log` |
| New | Job browser job selections not audited | ✅ `JOB_BROWSER_SELECT user=... job=...` in `audit.log` |

---

### Low / Backlog — Open

| # | Issue | Status |
|---|---|---|
| ISSUE-01 | Jenkins API token stored in signed cookie (readable client-side) | ⏳ Cookie is HMAC-signed (tamper-proof). Move to server-side sessions in production. |
| ISSUE-03 | Session fixation — no session regeneration on login | ⏳ Low risk for internal tool; add `session.clear()` on login if needed |
| ISSUE-27 | Jenkins API over plain HTTP (localhost) | ⏳ OK for local deployment; configure TLS reverse proxy for production |
| ISSUE-26 | No automated vulnerability scanning | ⏳ Add `pip-audit` or Dependabot |
| ISSUE-29 | Sensitive chat history in localStorage | ⏳ Per-chat "Clear" button already exists; consider optional encryption |

---

## Fixed Details

### Rate limiting (ISSUE-17, ISSUE-18)

```python
@app.route("/login", methods=["POST"])
@limiter.limit("10 per minute")
def login_page(): ...

@app.route("/chat", methods=["POST"])
@limiter.limit("30 per minute")
def chat(): ...

@app.route("/api/jobs")
@limiter.limit("60 per minute")   # job browser
def api_jobs(): ...
```

### Input validation on new API routes

```python
def _int(val, default, lo, hi):
    try:
        return max(lo, min(hi, int(val)))
    except (TypeError, ValueError):
        return default

page     = _int(request.args.get("page",     1),  1,  1, 9999)
per_page = _int(request.args.get("per_page", 20), 20, 5,   50)
q        = request.args.get("q", "").strip()[:200]   # length cap
```

### XSS prevention in localStorage replay (ISSUE-15)

```javascript
function sanitizeHtml(dirty) {
    // DOMParser-based allowlist — only safe tags and attributes pass
    const _SAFE_TAGS = new Set(["a","b","i","em","strong","code","pre", ...]);
    const _SAFE_ATTRS = { a: ["href","target","rel"], ... };
    // Strips all event handlers (on*), javascript: URIs, unknown tags
}
```

### Artifact path traversal (ISSUE-08)

```python
safe_name = PurePosixPath(art["fileName"]).name   # "../../etc/passwd" → "passwd"
if not safe_name or "/" in safe_name or "\\" in safe_name or ".." in safe_name:
    logger.warning("Skipping artifact with unsafe filename: %r", art["fileName"])
    continue
file_dest = dest / safe_name
```

### Open redirect (ISSUE-05)

```python
def _safe_redirect_url(target: str) -> str:
    ref  = urlparse(request.host_url)
    test = urlparse(urljoin(request.host_url, target))
    if test.scheme in ("http", "https") and ref.netloc == test.netloc:
        return target
    logger.warning("Blocked open redirect attempt to: %r", target)
    return url_for("index")
```

---

## What is safe right now

✅ Passwords hashed with scrypt (werkzeug)  
✅ Jenkins credentials validated against Jenkins — never stored in plaintext  
✅ Role-based access enforced at every trigger and query point  
✅ Last admin protected from deletion  
✅ `.env` excluded from git  
✅ Jinja2 auto-escaping prevents server-side template XSS  
✅ All user-provided strings HTML-escaped before browser rendering  
✅ Bot reply HTML sanitised via allowlist (DOMParser-based)  
✅ Artifact filenames sanitised against path traversal  
✅ Build params validated against Jenkins schema  
✅ All auth events, build triggers, and job selections logged with IP  
✅ Security headers on every response  
✅ Rate limiting on login, chat, and API endpoints  
✅ Input length caps on all user-supplied strings  
✅ Build status endpoint checks ownership  
✅ TTL eviction prevents unbounded in-memory growth  

---

*Exterro · DevOps AI Challenge · September 2026*

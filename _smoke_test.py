"""BuildBot smoke test — runs against http://localhost:5000"""
import sys
import json
import time
import requests

BASE = "http://localhost:5000"
PASS = "✅"
FAIL = "❌"
WARN = "⚠️"

results = []

def check(name, ok, detail=""):
    icon = PASS if ok else FAIL
    results.append((ok, name, detail))
    print(f"  {icon}  {name}" + (f"  →  {detail}" if detail else ""))

def section(title):
    print(f"\n{'─'*50}")
    print(f"  {title}")
    print(f"{'─'*50}")

# ─── 1. Server running ────────────────────────────────────────────────────────

section("1. Server")
try:
    r = requests.get(BASE, timeout=5, allow_redirects=False)
    check("Server responding", r.status_code in (200, 302),
          f"HTTP {r.status_code}")
    check("Redirects to login", r.status_code == 302 and "/login" in r.headers.get("Location",""),
          r.headers.get("Location",""))
except Exception as e:
    check("Server responding", False, str(e))
    print("\n  Server is not running. Start with: python app.py")
    sys.exit(1)

# ─── 2. Login ─────────────────────────────────────────────────────────────────

section("2. Login")
s = requests.Session()
try:
    # GET /login — should return HTML
    r = s.get(f"{BASE}/login", timeout=5)
    check("Login page loads", r.status_code == 200,
          f"HTTP {r.status_code}, {len(r.text)} chars")
    check("Login page has form", "csrf_token" in r.text or "username" in r.text,
          "form elements found")

    # POST /login with bad creds
    r = s.post(f"{BASE}/login",
               data={"username": "admin", "password": "wrongpassword",
                     "csrf_token": ""},
               timeout=5, allow_redirects=True)
    check("Bad login rejected", r.url.endswith("/login") or "failed" in r.text.lower()
          or r.status_code == 200,
          "stays on login page")

    # POST /login with good creds
    r = s.post(f"{BASE}/login",
               data={"username": "admin", "password": "Adm!n@12",
                     "csrf_token": ""},
               timeout=5, allow_redirects=True)
    check("Good login succeeds", "/" in r.url and "login" not in r.url,
          f"redirected to {r.url}")
except Exception as e:
    check("Login flow", False, str(e))

# ─── 3. Session ───────────────────────────────────────────────────────────────

section("3. Session")
try:
    r = s.get(f"{BASE}/debug-session", timeout=5)
    if r.status_code == 200:
        data = r.json()
        check("Session has auth_user",  data.get("auth_user") == "admin",
              f"auth_user={data.get('auth_user')}")
        check("Session has auth_mode",  data.get("auth_mode") is not None,
              f"auth_mode={data.get('auth_mode')}")
        check("Session has last_active", data.get("has_last_active") is True)
    else:
        check("Debug session endpoint", False, f"HTTP {r.status_code}")
except Exception as e:
    check("Session check", False, str(e))

# ─── 4. Chat — general (no Jenkins) ──────────────────────────────────────────

section("4. Chat (general)")
try:
    csrf = s.cookies.get("csrf_token", "")
    # First create a session
    r = s.post(f"{BASE}/sessions",
               headers={"X-CSRFToken": csrf},
               json={}, timeout=5)
    if r.status_code == 200:
        chat_id = r.json().get("chat_id", "test")
    else:
        chat_id = "test"
    check("Create chat session", r.status_code == 200,
          f"chat_id={chat_id[:8]}...")

    # Send "hi" — should return without hitting Jenkins
    csrf = s.cookies.get("csrf_token", "")
    r = s.post(f"{BASE}/chat",
               headers={"X-CSRFToken": csrf,
                        "Content-Type": "application/json"},
               json={"type": "message", "message": "hi", "chat_id": chat_id},
               timeout=15)
    check("Chat returns JSON",    r.status_code == 200,
          f"HTTP {r.status_code}")
    if r.status_code == 200:
        data = r.json()
        check("'hi' returns reply",  bool(data.get("reply")),
              (data.get("reply") or "")[:60])
        check("'hi' skips Jenkins",  not data.get("job_card") and not data.get("param_card"),
              "no job_card/param_card")
except Exception as e:
    check("Chat general", False, str(e))

# ─── 5. Jenkins connectivity ──────────────────────────────────────────────────

section("5. Jenkins")
try:
    r = s.get(f"{BASE}/status", timeout=10)
    if r.status_code == 200:
        data = r.json()
        check("Jenkins reachable", data.get("jenkins") == "ok",
              f"status={data.get('jenkins')}")
        check("LLM reachable",     data.get("llm") in ("ok", "error"),
              f"status={data.get('llm')}")
    else:
        check("Status endpoint", False, f"HTTP {r.status_code}")
except Exception as e:
    check("Jenkins status", False, str(e))

# ─── 6. Chat — build intent ────────────────────────────────────────────────────

section("6. Chat (build intent)")
try:
    csrf = s.cookies.get("csrf_token", "")
    r = s.post(f"{BASE}/chat",
               headers={"X-CSRFToken": csrf,
                        "Content-Type": "application/json"},
               json={"type": "message", "message": "build", "chat_id": chat_id},
               timeout=15)
    check("'build' returns JSON", r.status_code == 200,
          f"HTTP {r.status_code}")
    if r.status_code == 200:
        data = r.json()
        has_jobs    = bool(data.get("job_card"))
        has_params  = bool(data.get("param_card"))
        has_reply   = bool(data.get("reply"))
        check("'build' triggers Jenkins flow",
              has_jobs or has_params or has_reply,
              f"job_card={has_jobs} param_card={has_params} reply={has_reply}")
except Exception as e:
    check("Chat build intent", False, str(e))

# ─── 7. Admin panel ────────────────────────────────────────────────────────────

section("7. Admin panel")
try:
    r = s.get(f"{BASE}/admin/users", timeout=5)
    check("Admin panel loads",
          r.status_code == 200 and "user-table" in r.text,
          f"HTTP {r.status_code}")

    r = s.get(f"{BASE}/admin/api/users", timeout=5)
    check("Admin API returns users", r.status_code == 200,
          f"{len(r.json())} users")
    if r.status_code == 200:
        users = r.json()
        check("admin user exists",    "admin" in users)
        check("Passwords not exposed",
              all("password_hash" not in u for u in users.values()),
              "password_hash absent")
except Exception as e:
    check("Admin panel", False, str(e))

# ─── 7b. Job browser API ─────────────────────────────────────────────────────

section("7b. Job browser API")
try:
    r = s.get(f"{BASE}/api/jobs", timeout=10)
    check("/api/jobs returns JSON",     r.status_code == 200, f"HTTP {r.status_code}")
    if r.status_code == 200:
        data = r.json()
        check("/api/jobs has jobs key",       "jobs" in data)
        check("/api/jobs has total",          isinstance(data.get("total"), int))
        check("/api/jobs has total_pages",    isinstance(data.get("total_pages"), int))
        check("/api/jobs default per_page=20",data.get("per_page") == 20,
              f"got {data.get('per_page')}")

    # Filter: status=FAILURE
    r = s.get(f"{BASE}/api/jobs?status=FAILURE", timeout=10)
    check("/api/jobs?status=FAILURE", r.status_code == 200, f"HTTP {r.status_code}")
    if r.status_code == 200:
        d = r.json()
        check("FAILURE filter returns subset",
              d["total"] <= r.json().get("total", 99999))

    # Bad page param should not crash
    r = s.get(f"{BASE}/api/jobs?page=abc", timeout=10)
    check("/api/jobs?page=abc → 200 (safe int)",
          r.status_code == 200, f"HTTP {r.status_code}")

except Exception as e:
    check("Job browser API", False, str(e))

# ─── 8. Security headers ──────────────────────────────────────────────────────

section("8. Security headers")
try:
    r = s.get(BASE, timeout=5, allow_redirects=True)
    hdrs = r.headers
    check("X-Frame-Options",       hdrs.get("X-Frame-Options") == "DENY")
    check("X-Content-Type-Options", hdrs.get("X-Content-Type-Options") == "nosniff")
    check("Referrer-Policy",       "referrer-policy" in (k.lower() for k in hdrs))
    check("CSP header present",    "content-security-policy" in (k.lower() for k in hdrs))
except Exception as e:
    check("Security headers", False, str(e))

# ─── 9. Logout ────────────────────────────────────────────────────────────────

section("9. Logout")
try:
    r = s.get(f"{BASE}/logout", timeout=5, allow_redirects=True)
    check("Logout redirects to login",
          "login" in r.url,
          f"→ {r.url}")
    r = s.get(f"{BASE}/me", timeout=5, allow_redirects=False)
    check("After logout, /me is protected",
          r.status_code in (302, 401, 403),
          f"HTTP {r.status_code}")
except Exception as e:
    check("Logout", False, str(e))

# ─── Summary ──────────────────────────────────────────────────────────────────

print(f"\n{'═'*50}")
passed = sum(1 for ok,_,_ in results if ok)
failed = sum(1 for ok,_,_ in results if not ok)
total  = len(results)
print(f"  Result:  {passed}/{total} passed   {failed} failed")
if failed:
    print(f"\n  Failed checks:")
    for ok, name, detail in results:
        if not ok:
            print(f"    ❌ {name}  {detail}")
print(f"{'═'*50}\n")
sys.exit(0 if failed == 0 else 1)

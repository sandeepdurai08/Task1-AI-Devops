# BuildBot — Setup Guide

Complete step-by-step setup for a fresh Windows machine.
Total time: ~35 minutes.

---

## Prerequisites

| # | Tool | Version | Check |
|---|---|---|---|
| 1 | Python | 3.11+ | `python --version` |
| 2 | Java (JDK/JRE) | 17+ | `java -version` |
| 3 | Git | any | `git --version` |
| 4 | Jenkins LTS | 2.x | installed below |
| 5 | MailHog | latest | installed below |

> **Network:** Outbound HTTPS to the LLM endpoint (`LLM_URL` in `.env`) required for AI features.
> If the endpoint uses a self-signed certificate, set `LLM_VERIFY_SSL=false` in `.env`.

---

## Step 1 — Clone and install Python packages

```powershell
git clone <repo-url>
cd NEW-ai-bot-atmpt2

# Recommended: virtual environment
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

---

## Step 2 — Configure `.env`

```powershell
copy .env.example .env
```

Open `.env` and fill in each section:

### LLM
```env
LLM_URL=https://your-llm-endpoint/v1/chat/completions
LLM_MODEL=your-model-identifier
LLM_API_KEY=none
# Set to false if the endpoint uses a self-signed / internal TLS certificate
LLM_VERIFY_SSL=false
```

### Jenkins — service account
```env
JENKINS_URL=http://localhost:8080
JENKINS_USER=admin
JENKINS_TOKEN=<api-token>   # API token, NOT password — see Step 4

# Optional job allowlist (leave empty to expose all jobs visible to service account)
JENKINS_JOBS=
```

### Auth
```env
AUTH_MODE=jenkins       # jenkins or local
AUTH_FALLBACK=true      # keeps local admin working when Jenkins is down
```

### Defaults (developers can override in chat)
```env
DEFAULT_REPO_URL=https://github.com/your-org/your-repo
DEFAULT_BRANCH=main
```

### Artifacts
```env
ARTIFACT_SHARE=C:\shared\builds
```

### Notifications
```env
GCHAT_WEBHOOK=https://chat.googleapis.com/v1/spaces/...
SMTP_HOST=localhost
SMTP_PORT=1025
NOTIFY_EMAIL_FROM=buildbot@local
NOTIFY_EMAIL_TO=dev@local
```

### Flask
```env
# Generate: python -c "import secrets; print(secrets.token_hex(32))"
FLASK_SECRET_KEY=<64-char hex>
FLASK_DEBUG=1   # remove in production
```

---

## Step 3 — Jenkins setup (~15 minutes)

### 3.1 Install Jenkins
1. Install Java 17+ from [adoptium.net](https://adoptium.net/)
2. Download [Jenkins LTS .war](https://www.jenkins.io/download/)
3. Start: `java -jar jenkins.war --httpPort=8080`
4. Open **http://localhost:8080** → unlock with printed password
5. Install **suggested plugins**

### 3.2 Install Role-based Authorization Strategy
1. **Manage Jenkins → Plugins → Available**
2. Search `Role-based Authorization Strategy` → Install → Restart
3. **Manage Jenkins → Configure Global Security → Authorization → Role-Based Strategy** → Save

### 3.3 Create roles
**Manage Jenkins → Manage and Assign Roles → Manage Roles**

| Role | Overall | Job | View |
|---|---|---|---|
| `admin-role` | Administer ✅ | All ✅ | All ✅ |
| `developer-role` | Read ✅ | Build, Cancel, Read ✅ | Read ✅ |
| `readonly-role` | Read ✅ | Read ✅ | Read ✅ |

Optional: **Project roles** to restrict users to specific jobs (pattern: `hotfix-.*`).

### 3.4 Create and assign users
- **Manage Jenkins → Manage Users → Create User**
- **Manage and Assign Roles → Assign Roles** → map each user to a role

### 3.5 Create parameterised job
1. **New Item** → `DOTNET service` (or any name) → Freestyle project
2. ☑ **This project is parameterised** → Add:

| Type | Name | Default |
|---|---|---|
| String | `GITHUB_URL` | *(blank)* |
| String | `BRANCH` | `main` |
| Boolean | `RUN_TESTS` | `true` |
| Choice | `MODULES` | `ALL` / `Payments.Core` / `Payments.Api` |

3. **Build Steps → Execute Windows batch:**
   ```bat
   echo Building %GITHUB_URL% branch %BRANCH%
   echo Modules: %MODULES%   Tests: %RUN_TESTS%
   ```
4. **Post-build Actions → Archive the artifacts:** `**/*.dll, **/*.jar`
5. Save.

> BuildBot reads the parameter schema at runtime — add any parameters freely.

### 3.6 Service account API token
1. Log in as the service account → username → **Configure → API Token → Add new Token**
2. Name it `buildbot-service` → Generate → copy
3. Paste into `JENKINS_TOKEN` in `.env`

### 3.7 User API tokens
Each developer:
1. Logs into Jenkins → their name → **Configure → API Token → Add new Token**
2. Uses this token to log into BuildBot (more secure than password)

---

## Step 4 — MailHog (local email, ~2 minutes)

1. Download `MailHog.exe` from [github.com/mailhog/MailHog/releases](https://github.com/mailhog/MailHog/releases/latest)
2. Run it (double-click or `.\MailHog.exe`)
3. Web inbox: **http://localhost:8025** — SMTP on `localhost:1025`

---

## Step 5 — Run BuildBot

```powershell
.venv\Scripts\Activate.ps1
python app.py
```

Open **http://localhost:5000** → log in with Jenkins credentials.

---

## Step 6 — Verify each component

### UI only (no Jenkins)
```
GET http://localhost:5000/test-card        → param card
GET http://localhost:5000/test-job-picker  → job picker
```

### Jenkins connectivity
```powershell
python -c "
from dotenv import load_dotenv; load_dotenv()
from services.jenkins_client import check_jenkins_alive, list_jobs
print('alive:', check_jenkins_alive())
print('jobs:', [j['name'] for j in list_jobs()])
"
```

### LLM connectivity
The `/status` endpoint checks LLM reachability. Open **http://localhost:5000/status**
and verify `"llm": "ok"`. The header pills also show live status.

### Full LLM test
```powershell
python -c "
from dotenv import load_dotenv; load_dotenv()
from services.llm_client import detect_intent, general_chat_response
print(detect_intent('build hotfix/PAY-1 from https://github.com/acme/repo'))
print(detect_intent('who triggered the DOTNET service'))
print(detect_intent('hi'))
print(general_chat_response('hi'))
"
```

### Email
```powershell
python -c "
from dotenv import load_dotenv; load_dotenv()
from services.notifier import send_email
send_email('BuildBot test', 'Email is working.')
print('Check http://localhost:8025')
"
```

---

## Step 7 — Admin panel

Open **http://localhost:5000/admin/users** (requires `admin` role user).  
The **👥 Users** link in the header is only visible to admins.

| Action | Available |
|---|---|
| View all users | ✅ Table with role badges, allowed jobs, status |
| Add user (local account) | ✅ Form with live password policy |
| Edit user | ✅ Display name, role, allowed jobs |
| Change password | ✅ Live policy checklist |
| Disable / Enable | ✅ Blocks login, keeps history |
| Delete | ✅ Protected: last admin cannot be deleted |

---

## `.env` Reference

| Key | Required | Dev-overridable | Description |
|---|---|---|---|
| `LLM_URL` | ✅ | ❌ | LLM endpoint (`/v1/chat/completions`) |
| `LLM_MODEL` | ✅ | ❌ | Model identifier |
| `LLM_API_KEY` | ✅ | ❌ | API key (`none` for internal) |
| `LLM_VERIFY_SSL` | ✅ | ❌ | `true`/`false` — skip TLS cert check |
| `JENKINS_URL` | ✅ | ❌ | Jenkins base URL |
| `JENKINS_USER` | ✅ | ❌ | Service account username |
| `JENKINS_TOKEN` | ✅ | ❌ | Service account API token |
| `JENKINS_JOBS` | optional | ❌ | Job allowlist (empty = all visible) |
| `AUTH_MODE` | ✅ | ❌ | `jenkins` or `local` |
| `AUTH_FALLBACK` | ✅ | ❌ | `true` enables local admin fallback |
| `DEFAULT_REPO_URL` | optional | ✅ | Default repo URL for builds |
| `DEFAULT_BRANCH` | optional | ✅ | Default branch |
| `ARTIFACT_SHARE` | ✅ | ❌ | Artifact output folder |
| `GCHAT_WEBHOOK` | optional | ❌ | Google Chat webhook URL |
| `SMTP_HOST` | ✅ | ❌ | SMTP host (default: `localhost`) |
| `SMTP_PORT` | ✅ | ❌ | SMTP port (default: `1025`) |
| `NOTIFY_EMAIL_FROM` | ✅ | ❌ | Sender address |
| `NOTIFY_EMAIL_TO` | ✅ | ❌ | Recipient(s), comma-separated |
| `FLASK_SECRET_KEY` | ✅ | ❌ | 64-char random hex |
| `FLASK_DEBUG` | optional | ❌ | `1` for auto-reload (dev only) |
| `SESSION_IDLE_TIMEOUT_MIN` | optional | ❌ | Idle logout timeout (default: 60) |
| `ENTRY_TTL_HOURS` | optional | ❌ | In-memory TTL (default: 24) |
| `RATE_LOGIN` | optional | ❌ | Login rate limit (default: `10 per minute`) |
| `RATE_CHAT` | optional | ❌ | Chat rate limit (default: `30 per minute`) |
| `COOKIE_SECURE` | optional | ❌ | `1` behind TLS reverse proxy |

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `⚠️ Cannot reach Jenkins` | Start Jenkins: `java -jar jenkins.war --httpPort=8080` |
| `403 Forbidden` from Jenkins | Using password instead of API token — regenerate token |
| Login fails | Verify: `services.auth.verify_jenkins_credentials('user','token')` |
| No jobs shown | User lacks `Job/Read` in Jenkins — assign `developer-role` |
| Build returns 403 | User has Read but not Build permission — add `Job/Build` |
| LLM pill shows red | Check `LLM_VERIFY_SSL=false` for self-signed certs; verify endpoint is up |
| Chat hangs | LLM endpoint slow — 30B model may take 30–120s; check `/status` |
| Email not arriving | Start MailHog — check http://localhost:8025 |
| `ModuleNotFoundError` | Run `pip install -r requirements.txt` |
| Build result stays amber/unknown | Server restarted mid-build (debug reload); re-trigger to confirm |
| Query goes to build flow | Phrase not in `_QUERY_RE` — open an issue with the exact phrase |

---

*Exterro · DevOps AI Challenge · September 2026*

# BuildBot — Chat-driven Jenkins Automation

**Exterro DevOps AI Challenge · September 2026**

BuildBot removes the human from the Jenkins operations loop. A developer types plain
English; the AI layer understands it, either triggers a build or answers a Jenkins query
(who triggered, console logs, artifacts, failures, views, permissions…), and responds in
the chat window.

---

## Quick Start

```powershell
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure
copy .env.example .env
# → fill in JENKINS_URL, JENKINS_USER, JENKINS_TOKEN, LLM_URL

# 3. Start
python app.py

# 4. Open browser
start http://localhost:5000
# → log in with your Jenkins username + API token
```

See **[SETUP.md](SETUP.md)** for the complete guide including Jenkins role setup.

---

## What you can say

### Triggering a build
```
build hotfix/PAY-4821 from https://github.com/acme/payments-api
build release/2.3.1 from https://github.com/acme/repo, skip tests, modules: Core, Api
need a hotfix build, just give me Payments.Core.dll
```

### Jenkins info queries
```
list jobs
list views
jobs in main view
search jobs payments
who triggered the last build of DOTNET service
who built the JAVA service             ← natural-language variants all work
check who build the DOTNET             ←
show console log for DOTNET service
show console log for csharp build 5 last 30 lines
why did DOTNET service fail
analyze why JAVA service failed
show artifacts for DOTNET service build 3
stop build 5 of JAVA service
kill the build
my permissions
show SFTP upload paths for HOT_fix_job
show running builds
show build history of csharp last 5
last 5 builds of JAVA service
retry build 3 of csharp
rebuild the last build of DOTNET service
show Jenkins queue
pending builds
show Jenkins agents
are any nodes offline
show git commits for DOTNET service build 8
what changed in csharp
compare build 5 and 6 of DOTNET service
show failed builds for JAVA service
failure history of csharp last 20
Jenkins version
Jenkins health
show installed plugins
```

---

## Features

### Core build automation (Level 1)
- Chat UI with dark-to-light theme, multi-chat sidebar, message history across refresh
- LLM (Qwen3-30B via Exterro endpoint) extracts build parameters from plain English
- Dynamic Jenkins job discovery — N jobs, zero config changes
- Interactive parameter card (checkboxes, text, choice lists, bidirectional sync)
- Background build thread — non-blocking HTTP responses
- Live build progress card (stage pipeline, elapsed timer, Jenkins link)
- Artifact copy to shared path; artifact paths rendered as clickable `file:///` links
- Build progress card reconnects after browser refresh (localStorage)

### Jenkins query engine (new)
Ten natural-language queries routed without triggering a build:

| Say | Does |
|---|---|
| `list jobs` / `search jobs <kw>` | Lists all jobs or filters by keyword |
| `list views` | Shows all Jenkins dashboard views |
| `jobs in <view> view` | Lists jobs inside a specific view |
| `who triggered/built/ran <job>` | Returns the user who started the last (or specific) build |
| `show running builds` | Lists all builds currently in progress |
| `show build history of <job> [last N]` | Last N builds with result, duration, timestamp |
| `retry build <N> of <job>` | Triggers a rebuild of a specific build |
| `show console log for <job> [build N] [last N lines]` | Fetches and displays console output |
| `analyze why <job> failed` | Last 20 error lines + LLM root-cause analysis |
| `show artifacts for <job> [build N]` | Lists artifacts with direct download links |
| `show SFTP upload paths for <job>` | Scans console for SFTP/SCP destinations |
| `stop/abort/kill build N of <job>` | Sends abort signal to Jenkins |
| `show Jenkins queue` | What's waiting to build and why |
| `show Jenkins agents` | Node online/offline status and executor counts |
| `show git commits for <job> [build N]` | Git changeset for a build |
| `compare build <N> and <M> of <job>` | Side-by-side result/duration/changes table |
| `show failed builds for <job> [last N]` | Filtered failure history |
| `Jenkins version` / `Jenkins info` | Server version, executor stats, offline nodes |
| `show installed plugins` | All plugins with versions and update availability |
| `my permissions` | Returns your Jenkins authority list |

### Selective artifact staging (Level 2)
- "just give me Payments.Core.dll" — understood and applied
- Selective staging shown inline with file list and hyperlinks

### Failure diagnosis
- LLM-powered root cause (20s timeout) with 8-pattern regex fallback
- Last N error-relevant console lines shown in collapsible card section
- Analysis visible whether triggered by build completion or direct query

### Authentication & access control
- **Jenkins-based login** — users sign in with Jenkins credentials
- Permissions auto-sync from Jenkins roles
- Personal Jenkins credentials used for build triggers (Jenkins enforces its own permissions)
- Emergency local admin fallback via `config/users.json`
- Web admin panel at `/admin/users` — add, edit, disable, delete users
- Password policy: 8+ chars, upper + lower + digit + special

### Security
- Server-side sessions (signed-cookie, 8h lifetime)
- CSRF protection (Flask-WTF, form-only mode)
- Rate limiting: 10/min login, 30/min chat
- Security headers on every response (CSP, X-Frame-Options, etc.)
- TTL eviction for in-memory state (24h)
- Build ownership check on `/build-status/<id>`
- Artifact path traversal prevention
- Open redirect protection

### UX
- Multi-chat sidebar — switch between conversations
- Chat history persists across browser refresh (localStorage)
- Build card reconnects after refresh
- Input stays enabled while build runs — trigger multiple builds simultaneously
- Typing indicator
- Intent detection — "hi" and query phrases don't trigger Jenkins builds
- Markdown rendering in bot messages (**bold**, `code`, fenced ` ``` ` blocks)
- Console log output rendered in dark-theme code block
- Rich Google Chat notifications (cardsV2 format)
- Faster polling (2s queue, 3s build)

---

## Project Structure

```
NEW-ai-bot-atmpt2/
├── app.py                   Flask app — routes, chat phases, query dispatcher, build threads
├── services/
│   ├── auth.py              Authentication (Jenkins + local fallback), decorators
│   ├── llm_client.py        LLM: intent detection, query parsing, param extraction, failure analysis
│   ├── jenkins_client.py    Jenkins REST: all API calls + in-memory job/param caches
│   └── notifier.py          Google Chat webhook + MailHog email
├── config/
│   └── users.json           Emergency local admin accounts
├── templates/
│   ├── index.html           Chat UI — all card types, console log rendering, info card
│   ├── login.html           Login page
│   └── admin_users.html     Admin panel
├── static/
│   ├── style.css            UI styles including build card, console, code block, info card
│   └── favicon.svg
├── scripts/
│   ├── manage_users.py      CLI user management
│   └── check_services.ps1   Pre-flight connectivity checks
├── docs/
│   ├── ARCHITECTURE.md      Component map, data flows, route list, Jenkins API table
│   ├── PROMPTS.md           All LLM prompts with rationale and examples
│   ├── RESPONSES.md         Predefined responses and LLM dependency map
│   ├── SECURITY.md          Security audit and hardening notes
│   └── USER_MANAGEMENT.md   Managing users (Jenkins + local mode)
├── SETUP.md                 Full step-by-step setup guide
├── .env                     Secrets (git-ignored)
└── .env.example             Template — safe to commit
```

---

## Authentication modes

| Mode | Who logs in | Permissions from |
|---|---|---|
| `AUTH_MODE=jenkins` | Jenkins username + API token | Jenkins roles (automatic) |
| `AUTH_MODE=local` | `config/users.json` accounts only | `users.json` allowed_jobs |

Set in `.env`. `AUTH_FALLBACK=true` keeps the local admin working even when Jenkins is down.

---

## UI walkthrough

**1 — Login**
```
Jenkins Username:              alice
Jenkins API Token:             ••••••••••••
                   [ Sign In ]
```

**2 — Build request**
> *"build hotfix/PAY-4821 from https://github.com/acme/payments-api"*

**3 — Job picker** (if multiple jobs match)

**4 — Parameter card** (pre-filled by LLM, editable)

**5 — Live build progress**
```
⚙️  Build in progress   1m 23s
[DOTNET service]  Build #8   🔗 Open in Jenkins
●────────●────────○
Queuing  Building  Complete
```

**6 — Completion**
```
✅  Build #8 SUCCESS  (40s)
📁 Artifacts: C:\shared\builds\8  ← clickable link
```

**7 — Failure**
```
❌  Build #9 FAILURE  (1m 2s)
⚠️ Cause: Compilation error in Payments.Core
💡 Check the console output for the failing file and line number.
▶ Show last 20 log lines    ← collapsible
```

**8 — Query**
> *"who built the DOTNET service"*
```
The last build of DOTNET service was triggered by alice
(cause: Started by user alice)  🔗 Open in Jenkins
```

---

## Dev helper routes

| Route | Purpose |
|---|---|
| `GET /test-card` | Hard-coded param card (no Jenkins needed) |
| `GET /test-job-picker` | Hard-coded job picker |
| `GET /build-status/<id>` | Live build state (polled by frontend) |
| `GET /debug-session` | Session state dump |
| `GET /me` | Current user JSON |
| `GET /status` | Jenkins + LLM connectivity status |

---

## LLM dependency — what works without it

| Feature | LLM needed? | Without LLM |
|---|---|---|
| Intent detection | ❌ No | Pure keyword regex — always works |
| Query intent routing | ❌ No | Keyword regex (`_QUERY_RE`) — always works |
| Job picker card | ❌ No | Jenkins API — always works |
| Build trigger | ❌ No | Jenkins API — always works |
| Live build progress | ❌ No | Jenkins polling — always works |
| Failure diagnosis | ❌ No | Regex patterns — always works |
| Notifications | ❌ No | HTTP + SMTP — always works |
| Jenkins query parsing | ✅ Yes | Falls back: "unknown" action → help text shown |
| Pre-filling build params | ✅ Yes | Param card empty — dev fills manually |
| Auto-selecting job | ✅ Yes | Falls back to job picker |
| General chat responses | ✅ Yes | Hardcoded fallback message shown |

---

## Documentation

| Document | Contents |
|---|---|
| [SETUP.md](SETUP.md) | Python, Jenkins, .env, auth modes, troubleshooting |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Component map, all data flows, route list, API table |
| [docs/PROMPTS.md](docs/PROMPTS.md) | All LLM prompts, settings, examples, fallback behaviour |
| [docs/RESPONSES.md](docs/RESPONSES.md) | Predefined responses, LLM dependency map |
| [docs/USER_MANAGEMENT.md](docs/USER_MANAGEMENT.md) | Jenkins roles, BuildBot admin panel |
| [docs/SECURITY.md](docs/SECURITY.md) | Security audit, fix status, hardening guide |

---

*Exterro · DevOps AI Challenge · September 2026*

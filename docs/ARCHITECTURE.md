# BuildBot — Architecture

## Overview

BuildBot is a single-server Flask application. All build logic runs in background
threads. The browser polls a status endpoint for live updates. A three-intent router
(`"build"` / `"query"` / `"chat"`) directs messages to the right handler without ever
blocking the Flask response.

---

## Component Map

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                               Browser                                       │
│                                                                             │
│  ┌──────────┐  ┌───────────────────────────────┐  ┌────────────────────┐  │
│  │Login page│  │  Chat UI (index.html)          │  │ Admin panel        │  │
│  └────┬─────┘  │  • param card                 │  │ (admin_users.html) │  │
│       │        │  • job picker card            │  └────────┬───────────┘  │
│       │        │  • build progress card        │           │               │
│       │        │  • console log (collapsible)  │           │               │
│       │        │  • info card (query results)  │           │               │
│       │        │  • markdown + code blocks     │           │               │
│       │        └──────────────┬────────────────┘           │               │
└───────┼─────────────────────────────────────────────────────┼──────────────┘
        │POST /login            │POST /chat                   │ /admin/api/*
        │                       │GET  /build-status/<id>      │
        ▼                       ▼                             ▼
┌───────────────────────────────────────────────────────────────────────────┐
│                           Flask  app.py                                   │
│                                                                           │
│  ┌─────────────────┐  ┌────────────────────────────────────────────────┐ │
│  │  Auth (auth.py) │  │  Chat router                                   │ │
│  │                 │  │                                                │ │
│  │  Jenkins mode:  │  │  detect_intent(msg)                           │ │
│  │   verify creds  │  │    "chat"  → general_chat_response()          │ │
│  │   fetch roles   │  │    "query" → _handle_query()  ←── NEW        │ │
│  │  Local fallback:│  │    "build" → _handle_message()               │ │
│  │   users.json    │  │                                                │ │
│  └─────────────────┘  │  _handle_query() actions:                     │ │
│                        │    list_jobs / search_jobs                    │ │
│                        │    list_views / list_jobs_in_view             │ │
│                        │    who_triggered / stop_build                 │ │
│                        │    list_artifacts / console_log               │ │
│                        │    sftp_path / analyze_failure                │ │
│                        │    permissions                                │ │
│                        │                                                │ │
│                        │  _handle_message() (build flow):              │ │
│                        │    list_jobs → select_job (LLM)              │ │
│                        │    get_job_parameters → parse_build_request  │ │
│                        │    → param_card                               │ │
│                        │                                                │ │
│                        │  _handle_job_select():                        │ │
│                        │    pending_query? → _resume_query()  ←── NEW │ │
│                        │    else          → _schema_to_param_card()   │ │
│                        └────────────────────────┬───────────────────── │ │
└─────────────────────────────────────────────────┼─────────────────────┘
                                                  │ background thread
              ┌───────────────────────────────────┼──────────────────────┐
              ▼                   ▼               ▼                      ▼
   ┌──────────────────┐  ┌────────────────────┐  ┌───────────────────────┐
   │  Exterro LLM     │  │  Jenkins :8080     │  │  Notifier             │
   │  (OpenAI-compat) │  │                    │  │  Google Chat webhook  │
   │                  │  │  Caches (in-memory)│  │  MailHog SMTP :1025   │
   │  detect_intent() │  │  list_jobs   30s   │  └───────────────────────┘
   │  parse_jenkins_  │  │  get_params  5min  │
   │  query()         │  │                    │
   │  select_job()    │  │  Service account:  │
   │  parse_build_    │  │  list_jobs()       │
   │  request()       │  │  get_job_params()  │
   │  llm_analyze_    │  │  list_views()      │
   │  build_failure() │  │  list_artifacts()  │
   │  general_chat_   │  │  get_console_log() │
   │  response()      │  │  get_build_trigger │
   │                  │  │  get_user_info()   │
   │  SSL: controlled │  │                    │
   │  by LLM_VERIFY_  │  │  User credentials: │
   │  SSL env var     │  │  trigger_build()   │
   │                  │  │  abort_build()     │
   └──────────────────┘  └────────────────────┘
```

---

## Intent Detection

`detect_intent(message)` — pure regex, no LLM call, runs in < 1ms:

```
message
  │
  ├─ matches _CHAT_RE   (greetings, who-are-you, thanks)  → "chat"
  │
  ├─ matches _QUERY_RE  (list/show/who/stop/console/why)  → "query"
  │
  ├─ matches _BUILD_RE  (build/deploy/trigger/branch/URL) → "build"
  │
  ├─ ≤ 4 words                                            → "chat"
  │
  └─ default                                              → "build"
```

`_QUERY_RE` is checked **before** `_BUILD_RE`. Phrases like "who built X" and
"stop build N" match query patterns first and never reach the build flow.

---

## Query Flow (`_handle_query`)

```
User message ("who triggered the DOTNET service")
  │
  ├─ check_jenkins_alive()
  ├─ list_jobs()           ← uses 30s cache
  ├─ list_views()          ← used to supply context to LLM
  │
  ├─ parse_jenkins_query(msg, job_names, view_names)  [LLM, max 80 tokens]
  │   returns {action, job_name, build_number, view_name, lines, search_query}
  │
  └─ action dispatcher:
       who_triggered   → get_build_trigger(job_name)
       list_jobs       → list_jobs() [cached]
       list_views      → list_views()
       list_jobs_in_view → list_jobs_in_view(view_name)
       stop_build      → abort_build(job_name, build_number)
       permissions     → get_current_user_info()
       list_artifacts  → list_build_artifacts(job_name, build_number)
       console_log     → get_console_log_tail(job_name, build_number, lines)
       sftp_path       → get_console_log_tail() + find_sftp_uploads()
       analyze_failure → get_console_log_tail() + llm_analyze_build_failure()
       search_jobs     → search_jobs(search_query, jobs)
       unknown         → help text listing supported queries
```

If the LLM cannot extract `job_name`, a **job picker card** is shown. When the user
clicks a job, `_handle_job_select` reads `pending_query` from the session and calls
`_resume_query` instead of the build flow.

---

## Build Flow (`_handle_message`)

```
User message ("build hotfix/PAY-1 from https://github.com/acme/repo")
  │
  ├─ check_jenkins_alive()
  ├─ list_jobs()           ← 30s cache
  │
  ├─ single job?  → skip LLM, go to _schema_to_param_card()
  │
  ├─ multiple jobs → select_job(msg, jobs)  [LLM]
  │   high confidence → _schema_to_param_card()
  │   low confidence  → job picker card (conv_sessions stores last_message)
  │
  └─ _schema_to_param_card(sid, job_name, msg)
       ├─ get_job_parameters(job_name)  ← 5min cache
       ├─ parse_build_request(msg, schema)  [LLM]
       ├─ _merge_params() — LLM > .env defaults > schema defaults
       └─ returns param_card or "still missing X" prompt
```

```
User submits param_submit
  │
  ├─ validate params against schema keys (injection prevention)
  ├─ extract user's Jenkins credential from session (before thread starts)
  └─ spawn _run_build() daemon thread:
       trigger_build(user_auth)  → queue_url
       poll_queue()              → build_number
       poll_build()              → result (SUCCESS / FAILURE / …)
       _copy_artifacts()         → staged files list (non-fatal on failure)
       llm_analyze_build_failure() [if failed, 20s timeout]
       send_gchat()              (non-fatal)
       send_email()              (non-fatal)
       _update_job(done=True)
```

---

## Job Picker — Dual Purpose

The same job picker card is used for both the build flow and query actions:

| Triggered by | `pending_query` in session? | `_handle_job_select` routes to |
|---|---|---|
| Build flow (low-confidence `select_job`) | No | `_schema_to_param_card()` → param card |
| Query action (no job_name extracted) | Yes | `_resume_query()` → query result |

---

## In-Memory State

### `_conv_sessions`
Keyed by `{flask_sid}:{chat_id}`.

```python
# Build flow
{
  "sid:chat": {
    "job_name":            "DOTNET service",
    "schema":              [...],
    "last_message":        "build hotfix/...",
    "requested_artifacts": ["Payments.Core.dll"],
  }
}

# Query flow (when job picker is shown for a query)
{
  "sid:chat": {
    "job_name":       None,
    "last_message":   "who triggered DOTNET",
    "available_jobs": [...],
    "pending_query": {
      "action":       "who_triggered",
      "build_number": None,
      "view_name":    None,
      "lines":        50,
      "search_query": None,
    },
  }
}
```

### `_build_jobs`
Keyed by UUID. 24h TTL eviction.

```python
{
  "uuid": {
    "status":             "queuing|queued|building|success|failed|error|unknown",
    "message":            str,
    "done":               bool,
    "owner":              "alice",
    "build_number":       8,
    "build_url":          "http://localhost:8080/job/DOTNET service/8/",
    "artifact_path":      "C:\\shared\\builds\\8",
    "staged_files":       [{"name": "...", "path": "..."}],
    "not_found_files":    [],
    "selective_staging":  bool,
    "console_tail":       ["last", "20", "lines"],
    "result":             "SUCCESS|FAILURE|ABORTED|UNSTABLE",
    "failure_cause":      str,
    "failure_suggestion": str,
  }
}
```

---

## Caches

| Cache | Location | TTL | Key | Purpose |
|---|---|---|---|---|
| `_jobs_cache` | `jenkins_client.py` | 30 seconds | global | `list_jobs()` — avoids 2 round-trips per message |
| `_params_cache` | `jenkins_client.py` | 5 minutes | `job_name` | `get_job_parameters()` — schema rarely changes |
| `_user_chats` | `app.py` | 24h TTL evict | `flask_sid` | Chat session metadata |

---

## URL Routes

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/` | `require_auth` | Chat UI |
| GET/POST | `/login` | — | Login page |
| GET | `/logout` | — | Clear session |
| GET | `/me` | `require_auth` | Current user JSON |
| POST | `/chat` | `require_auth` | Chat — all phases (message / job_select / param_submit) |
| GET | `/build-status/<id>` | ownership check | Live build state |
| GET | `/sessions` | `require_auth` | List chat sessions |
| POST | `/sessions` | `require_auth` | New chat session |
| PATCH | `/sessions/<id>` | `require_auth` | Rename session |
| DELETE | `/sessions/<id>` | `require_auth` | Delete session |
| GET | `/status` | `require_auth` | Jenkins + LLM connectivity pills |
| GET | `/debug-session` | — | Session state (dev) |
| GET | `/admin/users` | `require_admin` | Admin panel page |
| GET/POST | `/admin/api/users` | `require_admin` | List / create users |
| PATCH | `/admin/api/users/<u>` | `require_admin` | Update user |
| PUT | `/admin/api/users/<u>/password` | `require_admin` | Change password |
| DELETE | `/admin/api/users/<u>` | `require_admin` | Delete user |
| GET | `/admin/api/jenkins-jobs` | `require_admin` | Job list for admin panel |
| GET | `/test-card` | `require_auth` | Dev: param card without Jenkins |
| GET | `/test-job-picker` | `require_auth` | Dev: job picker without Jenkins |

---

## Jenkins REST API Calls

| Call | Auth | Path | Cached? |
|---|---|---|---|
| Liveness / verify creds | Service / User | `GET /api/json` | No |
| List all jobs | Service account | `GET /api/json?tree=jobs[name,description,color,url]` | 30s |
| List views | Service account | `GET /api/json?tree=views[name,url,jobs[name]]` | No |
| Jobs in a view | Service account | `GET /view/<name>/api/json?tree=jobs[...]` | No |
| Job param schema | Service account | `GET /job/<name>/api/json?tree=property[parameterDefinitions[...]]` | 5min |
| **Trigger build** | **User credentials** | `POST /job/<name>/buildWithParameters` | — |
| **Abort build** | **User credentials** | `POST /job/<name>/<n>/stop` (+ `/kill` fallback) | — |
| **Retry/rebuild** | **User credentials** | `POST /job/<name>/<n>/rebuild` | — |
| Poll queue | Service account | `GET <queue-url>/api/json` | — |
| Poll build | Service account | `GET /job/<name>/<n>/api/json` | — |
| Build trigger info | Service account | `GET /job/<name>/<n>/api/json?tree=number,url,actions[causes[...]]` | — |
| Build history | Service account | `GET /job/<name>/api/json?tree=builds[number,result,duration,...]` | — |
| Running builds | Service account | `GET /api/json?tree=jobs[name,builds[building,...]]` | — |
| Console log | Service account | `GET /job/<name>/<n>/consoleText` | — |
| Artifact list | Service account | `GET /job/<name>/<n>/api/json?tree=artifacts[...]` | — |
| Artifact download | Service account | `GET /job/<name>/<n>/artifact/<path>` | — |
| Build changesets | Service account | `GET /job/<name>/<n>/api/json?tree=changeSet[items[...]]` | — |
| Current user info | Service account | `GET /me/api/json?tree=id,fullName,authorities` | — |
| Last build number | Service account | `GET /job/<name>/api/json?tree=lastBuild[number]` | — |
| Jenkins queue | Service account | `GET /queue/api/json` | — |
| Jenkins agents | Service account | `GET /computer/api/json` | — |
| Jenkins server info | Service account | `GET /api/json?tree=version,...` + `GET /computer/api/json` | — |
| Plugins list | Service account | `GET /pluginManager/api/json?tree=plugins[...]` | — |

---

## Failure Diagnosis

Two-layer analysis: LLM (20s hard timeout) then regex fallback.

### Regex patterns (`analyze_console_failure`)

| Keyword(s) | Cause | Suggestion |
|---|---|---|
| `couldn't find remote ref`, `pathspec…did not match` | Branch not found | Check branch name and repo URL |
| `compilation failure`, `cannot find symbol`, `msbuild : error` | Compilation error | Check console for failing file/line |
| `tests run:`, `testcase.*failure`, `assertionerror` | Test failures | Run tests locally first |
| `OutOfMemoryError`, `gc overhead limit` | OOM | Increase executor memory |
| `connection refused`, `could not resolve host` | Network error | Check artifact repo/feed |
| `permission denied`, `403 forbidden` | Permission error | Check workspace permissions |
| `no space left on device`, `disk full` | Disk full | Free disk on agent |
| `timeout`, `build timed out` | Build timed out | Check for hanging processes |

---

## LLM Configuration

```env
LLM_URL=https://exterrollm.exterrocloud.info/v1/chat/completions
LLM_MODEL=/exterro/services/models/Qwen3-30B-A3B-Instruct-2507
LLM_API_KEY=none
LLM_VERIFY_SSL=false          # false = skip TLS cert check (self-signed internal cert)
```

HTTP client: `httpx.Client(verify=..., timeout=Timeout(connect=10s, read=120s))`

Failure analysis override: `timeout=20.0` per-call (so build result is never blocked > 20s).

`/no_think` is placed at the **start of the user message** (not the system prompt) on all
four LLM calls to suppress Qwen3's chain-of-thought and get fast responses.

---

*Exterro · DevOps AI Challenge · September 2026*

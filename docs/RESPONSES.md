# BuildBot — Responses Reference

Every message the bot can show, whether it requires the LLM,
and what happens when the LLM is unavailable.

---

## LLM Dependency Map

```
User sends a message
        ↓
detect_intent()   ← NO LLM — pure regex (three intents)
        │
        ├── "chat"  → general_chat_response()     ← LLM (fallback if unavailable)
        │
        ├── "query" → parse_jenkins_query()        ← LLM (fallback: help text)
        │             ↓ (20 actions dispatched here)
        │             list_jobs / search_jobs      → job browser card
        │             list_views / list_jobs_in_view
        │             who_triggered               ← Jenkins API
        │             stop_build / retry_build    ← Jenkins API (auth checked)
        │             list_artifacts              ← Jenkins API
        │             console_log                 ← Jenkins API
        │             sftp_path                   ← Jenkins API + regex scan
        │             analyze_failure             ← Jenkins API + LLM (fallback regex)
        │             list_running_builds         ← Jenkins API
        │             list_build_history          ← Jenkins API
        │             list_queue / list_agents    ← Jenkins API
        │             list_build_changes          ← Jenkins API
        │             compare_builds              ← Jenkins API
        │             search_failed_builds        ← Jenkins API
        │             get_jenkins_info            ← Jenkins API
        │             list_plugins                ← Jenkins API
        │             permissions                 ← Jenkins API
        │
        └── "build" → check Jenkins alive          ← NO LLM
                    → list_jobs()                  ← NO LLM (cached 30s)
                    → select_job()                 ← LLM (fallback: job picker)
                    → get_job_parameters()         ← NO LLM (cached 5min)
                    → parse_build_request()        ← LLM (fallback: empty card)
                    → param card shown             ← NO LLM
                    → trigger_build()              ← NO LLM (Jenkins API)
                    → poll_queue() / poll_build()  ← NO LLM (Jenkins polling)
                    → analyze_console_failure()    ← NO LLM (regex patterns)
                    → notifications                ← NO LLM (HTTP + SMTP)
```

---

## What works without the LLM

| Feature | Without LLM |
|---|---|
| Intent detection (chat/query/build) | ✅ Works — keyword regex always fires |
| Job browser (list, filter, sort, page) | ✅ Works — Jenkins API + Python filter |
| Job picker card | ✅ Works — Jenkins API |
| Param card (empty fields) | ✅ Works — dev fills manually |
| Triggering a build | ✅ Works — Jenkins API |
| Stop / retry build | ✅ Works — Jenkins API |
| Build history, running builds, queue | ✅ Works — Jenkins API |
| Failure diagnosis (regex) | ✅ Works — 8 regex patterns |
| Artifact staging + links | ✅ Works — Jenkins API |
| Console log retrieval | ✅ Works — Jenkins API |
| Git changesets for a build | ✅ Works — Jenkins API |
| Google Chat notification | ✅ Works — HTTP webhook |
| Email notification | ✅ Works — SMTP |
| Auth and permissions | ✅ Works — Jenkins + users.json |
| Pre-filling params from text | ❌ LLM required |
| Query parsing (which action to run) | ❌ LLM + regex fallback |
| Auto-selecting correct job | ❌ LLM required (falls back to picker) |
| General conversation | ❌ LLM required (falls back to hardcoded message) |
| Build failure LLM analysis | ❌ LLM required (regex fallback always available) |

---

## Predefined Responses — no LLM required

### Connectivity

```
⚠️ Cannot reach Jenkins at `http://localhost:8080`. Please make sure Jenkins is running.
⚠️ Could not list Jenkins jobs: {error}
⚠️ No Jenkins jobs found. Check JENKINS_USER permissions and JENKINS_JOBS allowlist.
```

### Job browser (new)

```
Showing 1–20 of 127 jobs.
Type: "next page", "failed only", "hotfix", "sort by failed", "clear filters"…
```

```
Showing 1–34 of 34 jobs  ·  status: FAILURE.
```

```
No jobs found — active filters: status: FAILURE, hotfix only.
Type `clear filters` to reset, or try a different search.
```

```
No jobs matched repo `acme/payments` in job names or parameter defaults.
Which parameter holds the repo URL in your jobs? (e.g. GITHUB_URL, GIT_URL, REPO_URL)
```

### Query actions

```
2 build(s) currently running:
• DOTNET service — build #9  (running for 1m 23s)  🔗

Last 10 builds — `csharp`:
• ✅ #12 — SUCCESS  27s  2026-09-28 09:45
• ❌ #11 — FAILURE  14s  2026-09-28 08:31
…

🔁 Rebuild triggered for `csharp` — repeating build #11.

2 build(s) in the Jenkins queue:
• `JAVA service` — waiting 45s  (waiting for next available executor)

3 Jenkins agent(s):
• 🟢 master  — Online  idle (2 executors)
• 🔴 agent-1  — Offline  Reason: Connection refused

4 commit(s) in `DOTNET service` build #8:
• `a1b2c3d4`  alice — Fix null reference in Payments.Core
• `e5f6g7h8`  bob — Update NuGet packages

Build comparison — `csharp`:
| | Build #11 | Build #12 |
|---|---|---|
| Result | ❌ FAILURE | ✅ SUCCESS |
| Duration | 14s | 27s |
| Changes | 2 commits | 1 commits |

Jenkins Server Info  🔗 Open Jenkins
• Version: `2.452`
• Executors: 3/6 busy
• Offline nodes: 0
```

### Job selection

```
I found {n} jobs. Which one should I trigger?
I found {n} jobs (I think it might be `{job_name}`, but I'm not sure). Which one?
```

### Build parameters

```
Almost there — I still need: `GITHUB_URL`, `BRANCH`. Could you provide those?

Using job `{job_name}`. Review the parameters below — click checkboxes, edit
fields, or type comma-separated values. Click Build Now when ready.

Noted — using `{new_url}` instead of default repo.
📦 Selective staging: only `Payments.Core.dll` will be copied.
```

### Auth and permissions

```
⛔ You don't have permission to trigger `{job_name}`. Contact your admin.
⚠️ Session expired — the build parameters were lost (server likely reloaded).
   Please send your build request again.
```

### Build execution

```
Triggering Jenkins build…
Build queued — waiting for an executor…
Build #8 started — building…
✅ Build #8 SUCCESS (40s). 📁 Artifacts: C:\shared\builds\8
❌ Build #9 FAILURE (1m 2s).
🛑 Stop signal sent to `DOTNET service` build #9.
```

### Failure diagnosis (8 regex patterns)

| Pattern | Cause | Suggestion |
|---|---|---|
| `couldn't find remote ref`, `pathspec...did not match` | Branch not found | Check branch name and repo URL |
| `compilation failure`, `cannot find symbol`, `msbuild : error` | Compilation error | Check console for failing file/line |
| `tests run:.*failures`, `testcase.*failure` | Test failures | Run tests locally first |
| `OutOfMemoryError`, `gc overhead limit exceeded` | OOM | Increase executor memory |
| `connection refused`, `could not resolve host` | Network error | Check artifact repo reachability |
| `permission denied`, `access denied`, `403` | Permission error | Check workspace permissions |
| `no space left on device`, `disk full` | Disk full | Free disk space on build agent |
| `timeout`, `timed out` | Build timed out | Check for hanging processes |

### Network / UI errors (browser-side)

```
⏳ Still processing — the LLM is taking longer than usual. Please wait…
⏱️ Request timed out (3 min). The LLM is taking very long.
❌ Network error: {message}. Check your connection and try again.
❌ Server error (500). Please try again.
⚠️ Session expired — Sign in again.
```

---

## LLM-generated responses

### General chat (`general_chat_response`)

Triggered by: greetings, questions, short messages with no build/query keywords.

**LLM fallback:**
```
Hi! I'm BuildBot — your DevOps assistant.
To trigger a build: "build hotfix/PAY-1 from https://github.com/acme/repo"
```

### Query parsing (`parse_jenkins_query`)

Triggered by: `detect_intent()` returning `"query"`.

**LLM fallback (regex):**
- "next/more" → `action: next`
- "prev/back" → `action: prev`
- "clear/reset" → `action: clear`
- "fail/broken/error" → `action: filter, status: FAILURE`
- "hotfix/hf" → `action: filter, hotfix: true`

**LLM unavailable fallback:**
```
I didn't quite understand that Jenkins request. Here's what I can help with:
• list jobs / list views / jobs in <view> view
• search jobs <keyword>
• who triggered the last build of <job>
• show running builds / build history of <job>
• retry build <N> of <job> / stop build <N> of <job>
• show console log for <job> [build N] [last N lines]
• analyze why <job> failed [build N]
• show artifacts for <job> [build N]
• show SFTP upload paths for <job>
• show Jenkins queue / show Jenkins agents
• show git commits for <job> [build N]
• compare build <N> and <M> of <job>
• show failed builds for <job>
• Jenkins version / show installed plugins
• my permissions
```

### Job auto-selection (`select_job`)

**LLM fallback:** `confidence: "low"` → job picker shown.

### Parameter extraction (`parse_build_request`)

**LLM fallback:** All required no-default fields marked missing → param card empty.

### Build failure analysis (`llm_analyze_build_failure`)

**Timeout:** 20s hard cap per call.  
**LLM fallback:** regex patterns (always available, `source: "regex"`).

---

## Intent Detection Keywords

### `_QUERY_RE` (checked before build keywords)

Covers 55+ patterns across 15 categories:
- List/view jobs and views
- Stop, retry, abort builds
- Who triggered/built/ran (natural language variants including `check who build X`)
- Console logs, output, last N lines
- Artifact location/path/download
- SFTP upload paths
- Failure analysis, root cause, what went wrong
- Build history, recent builds, running builds
- Jenkins queue, pending builds
- Jenkins agents/nodes/executors
- Git commits, changesets, changed files
- Build comparison
- Failed build history
- Jenkins version/info/health
- Plugins

### `_BUILD_RE` (checked after query, before default)

```
\bbuild\b  \btrigger\b  \bdeploy\b  \bhotfix\b  \brelease\b
\bcompile\b  \bpipeline\b  \bjob\b  \bbranch\b
\bartifact\b  \bdll\b  \bjar\b
https?://github\.com  https?://gitlab\.com  https?://bitbucket\.
/hotfix/  /release/  /feature/  /fix/
```

### `_CHAT_RE` (checked first)

```
^\s*(hi|hello|hey|good morning|howdy)
^\s*how (are you|do you|can you)
^\s*(thanks|thank you|thx|cheers)
```

Messages ≤ 4 words with no build/query keyword → `"chat"`.

---

*Exterro · DevOps AI Challenge · September 2026*

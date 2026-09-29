# BuildBot — LLM Prompts

All LLM system prompts, their settings, examples, and fallback behaviour.

---

## LLM Endpoint

```
URL:   $LLM_URL    (set in .env)
Model: $LLM_MODEL  (set in .env)
```

OpenAI-compatible API. Configured via `LLM_URL`, `LLM_MODEL`, `LLM_API_KEY`,
and `LLM_VERIFY_SSL` in `.env`.

HTTP client: `httpx.Client(verify=LLM_VERIFY_SSL, timeout=Timeout(connect=10s, read=120s))`

---

## `/no_think` placement

All prompts inject `/no_think` at the **start of the user message** (not the system prompt).
This is the correct placement for Qwen3 — it disables chain-of-thought per-request,
reducing response time from minutes to seconds.

```python
messages = [
    {"role": "system", "content": <system_prompt>},
    # optional: last 3 conversation turns (context)
    {"role": "user",   "content": "<prior user message>"},
    {"role": "assistant", "content": "<prior bot reply>"},
    # current message — always last
    {"role": "user",   "content": f"/no_think\n{user_message}"},  # ← correct
]
```

## Conversation context

All 4 LLM call functions (`general_chat_response`, `select_job`, `parse_build_request`,
`parse_jenkins_query`) now receive the last **3 conversation turns** (6 messages) as
context prepended before the current user message.

The frontend extracts these from `chats[chatId].messages`, strips HTML to plain text
(capped at 300 chars/message), and sends them as `context: [{role, content}]` in the
POST `/chat` payload.

**What this enables:**

| User says | Without context | With context |
|---|---|---|
| `"build again"` | No URL/branch — shows picker | Reuses repo/branch from previous message |
| `"stop it"` | No job reference → shows picker | Knows which job was last triggered |
| `"analyze that"` | No build reference | Uses the job/build discussed previously |
| `"why did it fail"` | Generic fallback | References the last failed build from context |

---

## LLM is used for six things

| Function | File | LLM call | Fallback |
|---|---|---|---|
| `detect_intent()` | llm_client.py | **No** — pure regex | — always works |
| `general_chat_response()` | llm_client.py | Yes | Hardcoded help message |
| `parse_jenkins_query()` | llm_client.py | Yes | `action:"unknown"` → help text |
| `parse_job_filter_update()` | llm_client.py | Yes | Regex fallback (next/prev/clear/fail/hotfix) |
| `select_job()` | llm_client.py | Yes | `confidence:"low"` → job picker |
| `parse_build_request()` | llm_client.py | Yes | All required fields missing → dev fills |
| `llm_analyze_build_failure()` | llm_client.py | Yes (20s timeout) | Regex pattern matching |

---

## Intent Detection — no LLM

`detect_intent(message)` runs three compiled regex patterns in order:

1. **`_CHAT_RE`** — greetings, "who are you", thanks → `"chat"`
2. **`_QUERY_RE`** — Jenkins management phrases → `"query"`  ← checked BEFORE build
3. **`_BUILD_RE`** — build/deploy/trigger/branch/URL → `"build"`

`_QUERY_RE` includes 50+ patterns covering natural language variants:

```python
# Who triggered / built variants
r'\bwho\s+(?:build|built|did|made|kicked|deployed|executed|run|pushed)\b',
r'\bcheck\s+who\b',        # "check who build the DOTNET"
r'\bfind\s+(?:out\s+)?who\b',

# Console / log variants
r'\bshow\s+(?:the\s+)?(?:logs?|output|console)\b',
r'\bget\s+(?:the\s+)?(?:logs?|output|console)\b',
r'\blast\s+\d+\s+lines?\b',   # "last 20 lines"

# Failure analysis variants
r'\bwhat\s+(?:went\s+wrong|caused|broke)\b',
r'\bwhy.*fail\b',  r'\broot\s+cause\b',

# … and many more
```

---

## Prompt 1 — General Chat

**When called:** `detect_intent()` returns `"chat"`.  
**Location:** `general_chat_response()` in `llm_client.py`

### System prompt
```
You are BuildBot, a DevOps assistant inside a Jenkins build tool.
Help with build questions and general DevOps chat. Be concise (2-3 sentences max).
To trigger a build, users type: "build hotfix/PAY-1 from https://github.com/acme/repo"
Do NOT trigger Jenkins builds yourself.
```

### Settings
- `temperature: 0.7`
- `max_tokens: 100`

### Example
**User:** `hi`  
**Bot:** `Hey! I'm BuildBot. To trigger a Jenkins build, type something like: "build hotfix/PAY-1 from https://github.com/acme/repo"`

### Fallback (LLM unavailable)
```
Hi! I'm BuildBot — your DevOps assistant.
To trigger a build, tell me something like: "build hotfix/PAY-1 from https://github.com/acme/repo"
```

---

## Prompt 2 — Jenkins Query Parser

**When called:** `detect_intent()` returns `"query"`.  
**Location:** `parse_jenkins_query()` in `llm_client.py`

### System prompt (+ live catalogue injected)
```
You are BuildBot. Parse a Jenkins management request into a JSON action object.
The user may phrase requests informally — map them to the closest action.

Actions available:
list_jobs | list_views | list_jobs_in_view | stop_build | who_triggered |
permissions | list_artifacts | console_log | sftp_path | analyze_failure |
search_jobs | list_running_builds | list_build_history | retry_build |
list_queue | list_agents | list_build_changes | compare_builds |
search_failed_builds | get_jenkins_info | list_plugins | unknown

Common phrasings:
- "who build/built/ran/triggered/did" → who_triggered
- "check who build" → who_triggered
- "stop/abort/cancel/kill the build" → stop_build
- "retry/rebuild/run again" → retry_build
- "show/get/fetch/print logs/output/console" → console_log
- "why did it fail / what went wrong / root cause / analyze failure" → analyze_failure
- "show/list/get artifacts/files/dlls" → list_artifacts
- "artifacts" (alone), "artifact location of X", "need only artifact X" → list_artifacts
- "sftp/upload path/where was it uploaded" → sftp_path
- "list/show/get jobs" → list_jobs
- "list/show views" → list_views
- "jobs in <view> view/folder" → list_jobs_in_view
- "my permissions/access/role" → permissions
- "search/find job <keyword>" → search_jobs
- "running builds / what is building / currently building" → list_running_builds
- "build history / last N builds / recent builds" → list_build_history
- "Jenkins queue / pending builds / what is waiting" → list_queue
- "agents / nodes / executor status" → list_agents
- "what changed / git commits / changeset / changed files" → list_build_changes
- "compare build X and Y / diff builds" → compare_builds
- "failed builds / failure history / builds that failed" → search_failed_builds
- "Jenkins version / Jenkins info / Jenkins health" → get_jenkins_info
- "plugins / installed plugins" → list_plugins

Return ONLY a JSON object (no prose):
```json
{"action":"...","job_name":null,"build_number":null,"view_name":null,"lines":50,"search_query":null,"build_number_b":null,"count":10,"status":""}
```

Rules:
- `job_name` / `view_name` must be an exact name from the provided lists, or null.
- `build_number`: integer when user mentions a specific build, else null.
- `build_number_b`: second build number for `compare_builds`.
- `lines`: defaults to 50 for `console_log`, 20 for `analyze_failure`.
- `count`: builds to fetch for `list_build_history` / `search_failed_builds` (default 10).
- `search_query`: only for `search_jobs`.
- `status`: for `list_jobs` — `FAILURE|SUCCESS|BUILDING|UNSTABLE` if user filters by status (e.g. "failed jobs" → `FAILURE`). Empty = all jobs.
- If unclear, `action = "unknown"`.

Jobs: payments-build, java-service, dotnet-api, hotfix-deploy
Views: All, Failed Builds
```

### Settings
- `temperature: 0`
- `max_tokens: 80`

### Example interactions

**User:** `check who build the payments-build`  
**LLM output:**
```json
{"action":"who_triggered","job_name":"payments-build","build_number":null,"view_name":null,"lines":50,"search_query":null,"build_number_b":null,"count":10}
```

**User:** `show console log for java-service build 5 last 30 lines`  
**LLM output:**
```json
{"action":"console_log","job_name":"java-service","build_number":5,"view_name":null,"lines":30,"search_query":null,"build_number_b":null,"count":10}
```

**User:** `why did dotnet-api fail`  
**LLM output:**
```json
{"action":"analyze_failure","job_name":"dotnet-api","build_number":null,"view_name":null,"lines":20,"search_query":null,"build_number_b":null,"count":10}
```

**User:** `compare build 5 and 6 of payments-build`  
**LLM output:**
```json
{"action":"compare_builds","job_name":"payments-build","build_number":5,"view_name":null,"lines":50,"search_query":null,"build_number_b":6,"count":10}
```

**User:** `show failed builds for java-service last 15`  
**LLM output:**
```json
{"action":"search_failed_builds","job_name":"java-service","build_number":null,"view_name":null,"lines":50,"search_query":null,"build_number_b":null,"count":15}
```

### Post-processing
After parsing, `job_name` and `view_name` are validated against the actual lists.
Unknown names are nulled → job picker shown → user clicks → `_resume_query()` re-runs the action.

### Fallback (LLM unavailable / timeout)
Returns `{"action":"unknown"}` → bot shows a help message listing supported queries.

---

## Prompt 3 — Job Selection

**When called:** Multiple jobs exist and one must be chosen.  
**Location:** `select_job()` in `llm_client.py`

### System prompt (catalogue injected at runtime)
```
You are BuildBot. Select the single most appropriate Jenkins job for the developer's request.

Available jobs:
  - payments-build — builds the Payments service
  - java-service   — Java microservice build
  - dotnet-api     — .NET API build job

Return ONLY a JSON object:
{"job_name": "<exact name or null>", "confidence": "high" or "low", "reason": "<one sentence>"}

Rules: job_name must be from the list or null. high = one job clearly fits. No prose, raw JSON.
```

### Settings
- `temperature: 0`
- `max_tokens: 80`

### Example
**User:** `build hotfix/PAY-1 from https://github.com/acme/repo`  
**LLM:** `{"job_name":"payments-build","confidence":"high","reason":"Message mentions payments and hotfix, matching payments-build"}`

### Fallback (LLM unavailable)
Returns `{"job_name":null,"confidence":"low"}` → job picker shown → user clicks.

---

## Prompt 4 — Parameter Extraction

**When called:** Job selected, need to extract build parameters.  
**Location:** `parse_build_request()` in `llm_client.py`

### System prompt (schema injected at runtime)
```
You are BuildBot. Extract Jenkins build parameter values from the developer's message.

Job parameters:
  - GITHUB_URL (string, required — no default)
  - BRANCH     (string, required — no default)
  - RUN_TESTS  (boolean, default: true)
  - MODULES    (choice — one or more of: Payments.Core | Payments.Api | ALL, default: ALL)

Rules:
1. Return ONLY a JSON object — no prose, no markdown, no explanation.
2. Shape: {"params": {}, "missing": [], "requested_artifacts": []}
3. "params": every parameter you can determine. Use schema defaults for omitted params.
4. "missing": required params with no default that were not mentioned.
5. "requested_artifacts": specific filenames asked for (e.g. "Payments.Core.dll"). [] = copy all.
6. Booleans: true/false (JSON). Choices: JSON array using exact capitalisation from the list.
7. NEVER invent URLs or branch names — put them in "missing" if not stated.
```

### Settings
- `temperature: 0`
- `max_tokens: 200`

### Example interactions

**User:** `build hotfix/PAY-4821 from https://github.com/acme/payments-api, skip tests, just give me Payments.Core.dll`  
**Output:**
```json
{
  "params": {
    "GITHUB_URL": "https://github.com/acme/payments-api",
    "BRANCH": "hotfix/PAY-4821",
    "RUN_TESTS": false,
    "MODULES": ["ALL"]
  },
  "missing": [],
  "requested_artifacts": ["Payments.Core.dll"]
}
```

**User:** `build from https://github.com/acme/repo` (branch missing)  
**Output:**
```json
{
  "params": {"GITHUB_URL": "https://github.com/acme/repo", "RUN_TESTS": true, "MODULES": ["ALL"]},
  "missing": ["BRANCH"],
  "requested_artifacts": []
}
```

### Post-processing (code, not LLM)
```python
# 1. JSON extraction — 3 strategies (full string, ```fence, {…} substring)
# 2. One retry with "raw JSON only" instruction if parse fails
# 3. Choice normalisation — "payments.core" → "Payments.Core"
# 4. Boolean coercion — "true" (str) → True (bool)
# 5. Artifact list cleaned (empty strings removed)
```

### Fallback (LLM unavailable)
All required no-default fields go to `missing[]`. Bot asks dev to provide them.
Param card renders with empty required fields. Dev fills manually. Build triggers normally.

---

## Prompt 5 — Build Failure Analysis

**When called:** Build finished with non-SUCCESS result, or user asks `analyze why <job> failed`.  
**Location:** `llm_analyze_build_failure()` in `llm_client.py`  
**Timeout:** `20.0s` per-call override (so build result card is never blocked > 20s).

### System prompt
```
You are a DevOps expert. Analyse why a Jenkins build failed.
Respond ONLY with a JSON object:
{"cause": "<root cause, max 15 words>", "suggestion": "<fix, max 25 words>", "severity": "error"|"warning"|"info"}
Be specific — name the exact file or command that failed. Raw JSON only, no prose.
```

### Settings
- `temperature: 0`
- `max_tokens: 120`
- `timeout: 20.0` (per-call override)

### Fallback (LLM timeout / unavailable)
Falls back to `analyze_console_failure()` regex engine — 8 patterns covering the most
common build failures (branch not found, compile error, test failure, OOM, network,
permission, disk full, timeout). Result includes `"source":"regex"` vs `"source":"llm"`.

---

## Prompt 6 — Job Browser Filter Update

**When called:** User sends a message while the job browser card is active (navigating/filtering).  
**Location:** `parse_job_filter_update()` in `llm_client.py`

### System prompt (current filters injected)
```
You are BuildBot. Parse a job browser filter command.
Current filters: {"status":"","hotfix":false,"q":"","repo":"","page":1,"sort":"name"}

Return ONLY JSON:
{"action":"filter|next|prev|page_N|clear|select","status":"","hotfix":false,"q":"","repo":"","branch":"","sort":"","select_job":null}

action values:
- "filter": apply/change filters (status, hotfix, repo, branch, q, sort)
- "next": go to next page
- "prev": go to previous page
- "page_N": go to specific page (replace N with the number)
- "clear": reset all filters
- "select": user named a specific job (set select_job to the job name)

status values: FAILURE|SUCCESS|BUILDING|UNSTABLE|ALL (empty = no change)
sort values: name|status|failed|duration (empty = no change)
Leave fields empty/null/false if not mentioned.
```

### Settings
- `temperature: 0`
- `max_tokens: 80`

### Example interactions

**User:** `show failed only`  
**LLM:** `{"action":"filter","status":"FAILURE","hotfix":false,"q":"","repo":"","branch":"","sort":"","select_job":null}`

**User:** `next page`  
**LLM:** `{"action":"next"}`

**User:** `clear filters`  
**LLM:** `{"action":"clear"}`

**User:** `sort by recently failed`  
**LLM:** `{"action":"filter","status":"","hotfix":false,"q":"","repo":"","branch":"","sort":"failed","select_job":null}`

### Fallback (regex — fires when LLM unavailable or times out)

| Phrase | Result |
|---|---|
| "next", "more", "forward" | `action: next` |
| "prev", "back", "before" | `action: prev` |
| "clear", "reset", "all jobs" | `action: clear` |
| "fail", "broken", "error" | `action: filter, status: FAILURE` |
| "hotfix", "hf" | `action: filter, hotfix: true` |
| anything else | `action: filter` |

---

## Retry Logic

Both `select_job` and `parse_build_request` retry once on JSON parse failure:

```python
# First attempt
raw = _call()
result = _extract_json(raw)   # raises ValueError if no JSON found

# On failure — append LLM's bad output + correction prompt
if raw:
    messages.append({"role": "assistant", "content": raw})
messages.append({"role": "user", "content": "Return only the raw JSON object."})
raw = _call()
result = _extract_json(raw)

# On second failure — graceful fallback (missing all required fields)
```

---

## Design Decisions

| Decision | Reason |
|---|---|
| `/no_think` in user message | Correct Qwen3 placement — disables CoT, drops response time from minutes to seconds |
| `temperature: 0` for extraction | Deterministic output — same message → same parameters |
| `temperature: 0.7` for chat | Natural, varied conversational replies |
| Query regex checked before build regex | Prevents "who build X" from triggering the build flow |
| Schema injected at runtime | Prompt adapts to whatever parameters the Jenkins job has |
| `max_tokens` trimmed | 400→200 (parse), 150→80 (select/query), 200→100 (chat), 200→120 (failure) |
| 20s timeout for failure analysis | Build result shown promptly; analysis enriches it if fast enough |
| Regex fallback for failure | Always gives a useful diagnosis even without LLM |
| `confidence:"high"/"low"` | Binary simpler than a score. high = auto-proceed; low = show picker |
| `missing[]` field | Forces explicit listing of gaps — never trigger a build with a guessed URL |

---

## Prompt Iteration History

| Version | Problem | Fix |
|---|---|---|
| v1 | Qwen3 outputs 200 words of reasoning before JSON | Added `/no_think` |
| v2 | `/no_think` in system prompt — still slow | Moved to user message (correct placement) |
| v3 | URL extracted into BRANCH field | Added explicit rules to prompt |
| v4 | `confidence: 0.8` needed threshold tuning | Simplified to `"high"\|"low"` |
| v5 | Choice params returned as string `"A, B"` | Changed to array `["A", "B"]` |
| v6 | General chat ("hi") triggered Jenkins job list | Added `detect_intent()` + `_CHAT_RE` |
| v7 | "check who build X" triggered build flow | Added `_QUERY_RE` (checked before `_BUILD_RE`) |
| v8 | Query job picker routed to build | Added `pending_query` + `_resume_query()` |
| v9 | "artifacts" / "artifact location of X" routed to build | Added 8 standalone and `location/path/need` artifact patterns to `_QUERY_RE` |
| v10 | 10 new query actions added (build history, running builds, queue, etc.) | Extended `_QUERY_PARSE_SYSTEM` with 10 new actions + `build_number_b`/`count` fields |
| v11 | Job browser filter conversations needed LLM support | Added `parse_job_filter_update()` + `_JOB_FILTER_SYSTEM` (Prompt 6) |
| v12 | Real job names in docs replaced with generic examples | Documentation cleanup |
| v13 | "list failed job" triggered build flow | Added `\bfailed\s+jobs?\b`, `\blist\s+failed\b`, `\bshow\s+failed\s+jobs?\b`, `\bjobs?\s+that\s+failed\b` to `_QUERY_RE` |
| v14 — current | "list failed jobs" opened browser unfiltered | Added `status` field to `_QUERY_PARSE_SYSTEM`; `list_jobs` handler pre-applies status from LLM response |

---

*Exterro · DevOps AI Challenge · September 2026*

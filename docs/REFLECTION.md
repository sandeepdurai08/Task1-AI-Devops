# BuildBot — Reflection

**Exterro DevOps AI Challenge · September 2026**

---

## What the AI genuinely helped with

**Parameter extraction was the core win.** The single most error-prone step in the
old flow — turning "build hotfix/PAY-4821 from https://github.com/acme/repo" into
`{GITHUB_URL: "...", BRANCH: "hotfix/PAY-4821"}` — is exactly what an LLM does well.
It handles every natural-language variation without a single regex: "branch PAY-1",
"from the repo at ...", "skip tests this time", "just give me Payments.Core.dll".
Manual parsing would have needed hundreds of rules and still missed edge cases.

**Intent detection kept the bot reliable.** Routing "who built the DOTNET service" to
a query handler instead of a build trigger is harder than it looks — both sentences
mention a job name. Adding `_QUERY_RE` (checked before `_BUILD_RE`) caught 50+ natural
language variants without an LLM call. The LLM then only sees queries it genuinely
needs to parse; it never sees "hi" or "thanks".

**Failure diagnosis added real value beyond the spec.** Passing the last 20 error lines
to the LLM with `"name the exact file or command that failed"` produces actionable
output — "MSBuild failed on Payments.Core.csproj line 44" — not generic advice.
The 8-pattern regex fallback means users always get a diagnosis even when the LLM
is slow or unavailable.

**Conversation context ("build again")** worked out of the box once we started passing
the last 3 turns as `context[]`. "Same branch" and "that job" resolve correctly without
any custom memory layer.

---

## Where it got in the way

**JSON parsing was the biggest headache.** Qwen3 has a strong habit of wrapping output
in prose — "Sure! Here is the JSON you requested: ```json {...}```" — and of adding
chain-of-thought reasoning before the actual answer. This required three extraction
strategies (full string → fenced block → raw `{...}` substring scan), a one-retry
mechanism, and `/no_think` injected at the start of every user message. Without those
guards the bot was unreliable in production.

**`/no_think` placement was non-obvious.** The documentation for Qwen3's
chain-of-thought suppression says to put it in the prompt, but putting it in the
*system* prompt did not work — the model still reasoned at length. Moving it to the
start of the *user* message dropped response times from 60–90s to 3–5s. This cost
half a day to diagnose.

**Choice normalisation.** The LLM returned `"payments.core"` when the schema said
`"Payments.Core"`. Downstream validation silently dropped the value. We had to add a
case-insensitive fuzzy matcher to normalise choices before the param card was built.

**Ambiguous intent between build and query.** Early versions triggered a new build
when the user typed "who built DOTNET" because the sentence contained a job name and
the word "built". The fix (checking `_QUERY_RE` before `_BUILD_RE`) was simple once
identified, but finding it required reading a lot of accidental build logs.

---

## Which prompt version finally worked, and why

**For parameter extraction — v5** (returning choices as a JSON array rather than a
comma-separated string) was the turning point. Before that, `"Payments.Core, ALL"`
came back as a single string, which broke the choice-normalisation step and silently
omitted modules from the build. Switching to `["Payments.Core", "ALL"]` and adding
explicit rules ("Return choices as a JSON array") fixed it permanently.

**For query parsing — v7** (adding `_QUERY_RE` to intent detection) was the fix that
mattered. Before that, `parse_jenkins_query` was asked to distinguish queries from
build requests — it couldn't do it reliably. Moving the distinction to a fast regex
(no LLM, < 1ms) made the query path deterministic.

**For the LLM in general**, the combination that worked was:
1. `temperature: 0` (deterministic extraction, not creative writing)
2. `/no_think` in the user message (not system prompt)
3. One-retry on JSON parse failure with `"Return only the raw JSON object"`
4. Always validate + fallback in code — never trust the model to be the last safety net

---

## What we would do differently for a real production version

**Persist the build job state to a database.** `_build_jobs` is an in-memory dict with
a 24-hour TTL. In production, builds outlive process restarts; use Redis or a simple
SQLite table keyed by job UUID.

**Separate the LLM gateway from the Flask app.** The LLM calls block a Python thread
for up to 120 seconds. In a multi-user deployment, all threads saturate quickly under
load. A dedicated async gateway (FastAPI + httpx) with a request queue would isolate
LLM latency from the web tier.

**Add a proper audit trail.** The current `audit.log` records logins and key events
in a rotating file. For a production tool that triggers real deployments, every build
trigger should be stored in an immutable log with user, params, timestamp, and outcome
— queryable, not just grepped.

**Fine-tune on job names and repo patterns.** The generic LLM struggles with
organisation-specific abbreviations ("PAY", "DOTNET", "HOT_fix"). A few hundred
labelled examples would make intent detection and job selection dramatically more
accurate without requiring a prompt change.

**Replace polling with webhooks.** The frontend polls `/build-status/<id>` every 3s.
Jenkins supports post-build webhooks; a real implementation would use those and
Server-Sent Events on the Flask side to push updates, halving network chatter.

---

*Exterro · DevOps AI Challenge · September 2026*

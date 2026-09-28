"""
services/llm_client.py
─────────────────────
Calls the Exterro LLM (OpenAI-compatible endpoint) to extract structured
build parameters from a developer's plain-English message.

Public API
──────────
    parse_build_request(user_message, param_schema) -> dict
        Returns {"params": {...}, "missing": [...]}
"""

import json
import logging
import os
import re

import httpx
from openai import OpenAI, APIError

logger = logging.getLogger(__name__)

# ─── Client (singleton) ─────────────────────────────────────────────────────────

def _make_client() -> OpenAI:
    """Build an OpenAI-compatible client pointed at the Exterro LLM endpoint."""
    base_url = os.getenv("LLM_URL", "").rstrip("/")
    # OpenAI client expects base_url WITHOUT /chat/completions
    if base_url.endswith("/chat/completions"):
        base_url = base_url[: -len("/chat/completions")]

    api_key    = os.getenv("LLM_API_KEY", "none")   # Internal endpoint may not need a key
    verify_ssl = os.getenv("LLM_VERIFY_SSL", "true").strip().lower() != "false"

    if not verify_ssl:
        logger.warning(
            "LLM SSL verification is DISABLED (LLM_VERIFY_SSL=false). "
            "Only use this for internal / self-signed endpoints."
        )

    # 10s connect (fail fast if server is down), 120s read (30B model is slow).
    # Pass a custom httpx.Client so we can control SSL verification independently.
    http_client = httpx.Client(
        verify   = verify_ssl,
        timeout  = httpx.Timeout(connect=10.0, read=120.0, write=10.0, pool=5.0),
    )
    return OpenAI(base_url=base_url, api_key=api_key, http_client=http_client)


_client: OpenAI | None = None
_client_lock = __import__("threading").Lock()


def get_client() -> OpenAI:
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:          # double-checked locking
                _client = _make_client()
    return _client


# ─── Schema helpers ─────────────────────────────────────────────────────────────

def _schema_to_prompt_lines(param_schema: list[dict]) -> str:
    """
    Convert a list of Jenkins parameter definitions into a human-readable block
    for the system prompt so the LLM knows exactly what to look for.

    Example output:
        - GITHUB_URL  (string, required — no default)
        - BRANCH      (string, required — no default)
        - RUN_TESTS   (boolean, default: true)
        - MODULES     (choice — one or more of: Payments.Core | Payments.Api | Payments.Web | ALL, default: ALL)
    """
    lines = []
    for p in param_schema:
        ptype = p.get("type", "StringParameterDefinition")
        name  = p.get("name", "")
        default = p.get("default")

        if ptype == "BooleanParameterDefinition":
            default_str = f"default: {str(default).lower()}" if default is not None else "no default"
            lines.append(f"  - {name} (boolean, {default_str})")

        elif ptype == "ChoiceParameterDefinition":
            choices = " | ".join(p.get("choices", []))
            default_str = f"default: {default}" if default is not None else "no default"
            lines.append(f"  - {name} (choice — one or more of: {choices}, {default_str})")

        else:  # String or anything else
            if default is not None:
                lines.append(f"  - {name} (string, default: {default})")
            else:
                lines.append(f"  - {name} (string, required — no default)")

    return "\n".join(lines)


def _build_system_prompt(param_schema: list[dict]) -> str:
    schema_block = _schema_to_prompt_lines(param_schema)
    return f"""You are BuildBot. Extract Jenkins build parameter values from the developer's message.

Job parameters:
{schema_block}

Rules:
1. Return ONLY a JSON object — no prose, no markdown, no explanation.
2. Shape: {{"params": {{}}, "missing": [], "requested_artifacts": []}}
3. "params": every parameter you can determine. Use schema defaults for omitted params.
4. "missing": required params with no default that were not mentioned.
5. "requested_artifacts": specific filenames asked for (e.g. "Payments.Core.dll"). [] = copy all.
6. Booleans: true/false (JSON). Choices: JSON array using exact capitalisation from the list.
7. NEVER invent URLs or branch names — put them in "missing" if not stated."""


# ─── JSON extraction ─────────────────────────────────────────────────────────────

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
_LEADING_TEXT_RE = re.compile(r"^\s*[^{\[]*", re.DOTALL)


def _extract_json(raw: str) -> dict:
    """
    Best-effort extraction of a JSON object from LLM output.
    Handles markdown fences, leading prose, and trailing text.
    Raises ValueError if no valid JSON found.
    """
    # 1. Try the whole string first (ideal case)
    stripped = raw.strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    # 2. Pull out a ```json ... ``` fence if present
    fence_match = _JSON_FENCE_RE.search(stripped)
    if fence_match:
        try:
            return json.loads(fence_match.group(1).strip())
        except json.JSONDecodeError:
            pass

    # 3. Find the first { and last } and try that substring
    start = stripped.find("{")
    end   = stripped.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(stripped[start : end + 1])
        except json.JSONDecodeError:
            pass

    raise ValueError(f"No valid JSON object found in LLM output: {raw!r}")


# ─── Choice value normalisation ─────────────────────────────────────────────────

def _normalise_choices(raw_value, valid_choices: list[str]) -> list[str]:
    """
    Coerce an LLM choice value to a list of valid choice strings.
    raw_value may be: a list, a comma-separated string, or a single string.
    Matching is case-insensitive; output uses the exact capitalisation from valid_choices.
    """
    if not isinstance(raw_value, list):
        # e.g. "Payments.Core, Payments.Api" or "all"
        raw_value = [s.strip() for s in str(raw_value).split(",")]

    lower_map = {c.lower(): c for c in valid_choices}
    result = []
    for item in raw_value:
        normalised = lower_map.get(item.strip().lower())
        if normalised:
            result.append(normalised)
        else:
            logger.warning("LLM returned unknown choice value %r (valid: %s)", item, valid_choices)
    return result


# ─── Main public function ────────────────────────────────────────────────────────

def parse_build_request(user_message: str, param_schema: list[dict]) -> dict:
    """
    Ask the LLM to extract Jenkins parameter values from user_message.

    Parameters
    ----------
    user_message  : The developer's plain-English chat message.
    param_schema  : List of Jenkins parameter defs from get_job_parameters().
                    Each dict: {name, type, default, choices, description}

    Returns
    -------
    {
        "params":  {"PARAM_NAME": value, ...},   # extracted / defaulted values
        "missing": ["PARAM_NAME", ...],           # required params not found
    }

    Never raises — returns {"params": {}, "missing": [...all required params...]}
    on total failure so the bot can gracefully ask the dev for the missing info.
    """
    model   = os.getenv("LLM_MODEL", "")
    system  = _build_system_prompt(param_schema)
    # /no_think in the USER turn disables Qwen3 chain-of-thought (faster response)
    messages = [
        {"role": "system",  "content": system},
        {"role": "user",    "content": f"/no_think\n{user_message}"},
    ]

    def _call() -> str:
        client = get_client()
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=0,
            max_tokens=200,
        )
        return response.choices[0].message.content or ""

    # ── First attempt ────────────────────────────────────────────────────────────
    raw = ""
    try:
        raw = _call()
        result = _extract_json(raw)
    except (APIError, Exception) as exc:
        logger.warning("LLM first attempt failed (%s). Retrying with explicit instruction.", exc)
        # ── Re-prompt once ───────────────────────────────────────────────────────
        if raw:   # Fix: don't send empty assistant message — causes API rejection
            messages.append({"role": "assistant", "content": raw})
        messages.append({
            "role": "user",
            "content": (
                "Your response was not valid JSON. "
                "Return ONLY the raw JSON object with no other text, "
                "no markdown fences, no explanation."
            ),
        })
        try:
            raw = _call()
            result = _extract_json(raw)
        except Exception as exc2:
            logger.error("LLM second attempt also failed: %s. Raw output: %r", exc2, raw)
            # Fail gracefully — mark all required-no-default fields as missing
            required_missing = [
                p["name"] for p in param_schema
                if not p.get("default") and p["type"] != "BooleanParameterDefinition"
            ]
            return {"params": {}, "missing": required_missing, "requested_artifacts": []}

    # ── Post-process: normalise choice arrays, validate booleans ─────────────────
    params               = result.get("params", {})
    missing              = result.get("missing", [])
    requested_artifacts  = result.get("requested_artifacts", [])

    # Ensure requested_artifacts is a clean list of strings
    if not isinstance(requested_artifacts, list):
        requested_artifacts = []
    requested_artifacts = [str(f).strip() for f in requested_artifacts if str(f).strip()]

    schema_by_name = {p["name"]: p for p in param_schema}

    for name, value in list(params.items()):
        param_def = schema_by_name.get(name)
        if not param_def:
            continue

        ptype = param_def.get("type", "")

        if ptype == "ChoiceParameterDefinition":
            valid = param_def.get("choices", [])
            params[name] = _normalise_choices(value, valid)

        elif ptype == "BooleanParameterDefinition":
            if isinstance(value, str):
                params[name] = value.strip().lower() in ("true", "yes", "1", "on")
            else:
                params[name] = bool(value)

    logger.debug("parse_build_request → params=%s missing=%s artifacts=%s",
                 params, missing, requested_artifacts)
    return {"params": params, "missing": missing, "requested_artifacts": requested_artifacts}


# ─── Job selection ────────────────────────────────────────────────────────────────

def select_job(user_message: str, jobs: list[dict]) -> dict:
    """
    Ask the LLM to pick the most appropriate Jenkins job for the developer's request.

    Parameters
    ----------
    user_message : The developer's plain-English chat message.
    jobs         : List of job dicts from jenkins_client.list_jobs().
                   Each: {name, description, status}

    Returns
    -------
    {
        "job_name":   "hotfix-build",   # exact job name, or None if ambiguous
        "confidence": "high" | "low",
        "reason":     "The message mentions hotfix which matches hotfix-build",
    }

    confidence="low" or job_name=None means the bot should show a job picker card.
    Never raises — returns {"job_name": None, "confidence": "low", "reason": "..."} on failure.
    """
    model  = os.getenv("LLM_MODEL", "")

    # Build a concise job catalogue for the prompt
    job_lines = []
    for j in jobs:
        desc = f" — {j['description']}" if j.get("description") else ""
        job_lines.append(f"  - {j['name']}{desc}")
    catalogue = "\n".join(job_lines)

    system = f"""You are BuildBot. Select the single most appropriate Jenkins job for the developer's request.

Available jobs:
{catalogue}

Return ONLY a JSON object:
{{"job_name": "<exact name or null>", "confidence": "high" or "low", "reason": "<one sentence>"}}

Rules: job_name must be from the list or null. high confidence = one job clearly fits. No prose, raw JSON only."""

    messages = [
        {"role": "system", "content": system},
        {"role": "user",   "content": f"/no_think\n{user_message}"},
    ]

    def _call() -> str:
        client = get_client()
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=0,
            max_tokens=80,
        )
        return response.choices[0].message.content or ""

    raw = ""
    try:
        raw    = _call()
        result = _extract_json(raw)
    except Exception as exc:
        logger.warning("select_job first attempt failed (%s). Retrying.", exc)
        if raw:   # guard: don't append empty assistant message — causes API rejection
            messages.append({"role": "assistant", "content": raw})
        messages.append({
            "role": "user",
            "content": "Return only the raw JSON object with no other text.",
        })
        try:
            raw    = _call()
            result = _extract_json(raw)
        except Exception as exc2:
            logger.error("select_job failed twice: %s", exc2)
            return {
                "job_name":   None,
                "confidence": "low",
                "reason":     "Could not determine the job — LLM extraction failed.",
            }

    # Validate job_name is actually in the list
    valid_names = {j["name"] for j in jobs}
    job_name    = result.get("job_name")
    if job_name and job_name not in valid_names:
        logger.warning("LLM returned unknown job name %r — treating as ambiguous.", job_name)
        job_name = None

    return {
        "job_name":   job_name,
        "confidence": result.get("confidence", "low"),
        "reason":     result.get("reason", ""),
    }


# ─── Intent detection ────────────────────────────────────────────────────────────

# Keywords that strongly indicate a build/deploy request
_BUILD_KEYWORDS = [
    r'\bbuild\b', r'\btrigger\b', r'\bdeploy\b', r'\bhotfix\b',
    r'\brelease\b', r'\bcompile\b', r'\bpipeline\b', r'\bjob\b',
    r'\bbranch\b', r'\bartifact\b', r'\bdll\b', r'\bjar\b',
    r'https?://github\.com', r'https?://gitlab\.com', r'https?://bitbucket\.',
    r'/hotfix/', r'/release/', r'/feature/', r'/fix/',
    r'\btag\b', r'\bcommit\b', r'\bpr\b', r'\bpull.request\b',
    r'\bpayments\b', r'\bservice\b.*\bbuild\b',
]
_BUILD_RE = re.compile("|".join(_BUILD_KEYWORDS), re.IGNORECASE)

# Phrases that clearly indicate general conversation (not a build)
_CHAT_KEYWORDS = [
    r'^\s*(hi|hello|hey|good\s*(morning|afternoon|evening)|howdy)\b',
    r'^\s*how\s+(are\s+you|do\s+you|can\s+you)',
    r'^\s*what\s+(is|are|can|do)',
    r'^\s*who\s+are\s+you',
    r'^\s*(thanks|thank\s+you|thx|cheers)\b',
    r'^\s*(help|assist|support)\s*\??\s*$',
]
_CHAT_RE = re.compile("|".join(_CHAT_KEYWORDS), re.IGNORECASE)

# Keywords that indicate Jenkins management / info queries (not a build trigger)
# Written broadly to catch natural-language variants (past/present tense, check/find/get, etc.)
_QUERY_KEYWORDS = [
    # ── Job / view listing ──────────────────────────────────────────────────────
    r'\blist\s+(?:all\s+)?jobs?\b', r'\bshow\s+(?:all\s+)?jobs?\b',
    r'\bwhat\s+jobs?\b', r'\ball\s+(?:available\s+)?jobs?\b', r'\bget\s+jobs?\b',
    r'\blist\s+(?:all\s+)?views?\b', r'\bshow\s+(?:all\s+)?views?\b', r'\bwhat\s+views?\b',
    r'\bjobs?\s+in\s+(?:the\s+)?(?:view|folder|tab)\b', r'\bjobs?\s+under\b',
    r'\bjobs?\s+inside\b', r'\bjobs?\s+from\s+(?:the\s+)?view\b',
    r'\bjobs?\s+in\s+\w+\s+(?:view|folder)\b',  # "jobs in main view" (4-word form)

    # ── Stop / abort build ──────────────────────────────────────────────────────
    r'\bstop\s+(?:the\s+)?(?:build|job)\b', r'\babort\s+(?:the\s+)?(?:build|job)\b',
    r'\bcancel\s+(?:the\s+)?(?:build|job|run)\b', r'\bkill\s+(?:the\s+)?(?:build|job)\b',
    r'\bstop\s+it\b', r'\bkill\s+it\b', r'\bterminate\s+(?:the\s+)?(?:build|job)\b',
    r'\binterrupt\s+(?:the\s+)?(?:build|job)\b',

    # ── Who triggered ───────────────────────────────────────────────────────────
    r'\bwho\s+triggered\b', r'\bwho\s+started\b', r'\bwho\s+ran\b', r'\bwho\s+launched\b',
    r'\bwho\s+(?:build|built|did|made|kicked|deployed|executed|run|pushed)\b',  # "who build/built the job"
    r'\bwho\s+is\s+(?:building|running|deploying)\b',
    r'\bcheck\s+who\b',          # "check who build the DOTNET"
    r'\bfind\s+(?:out\s+)?who\b',  # "find out who ran it"

    # ── User permissions ────────────────────────────────────────────────────────
    r'\bmy\s+permissions?\b', r'\bmy\s+access\b', r'\bwhat\s+can\s+i\s+do\b',
    r'\baccess\s+level\b', r'\bmy\s+role\b', r'\bwhat\s+(?:am\s+i|role)\b',
    r'\bcheck\s+(?:my\s+)?(?:permissions?|access|role)\b',

    # ── Artifacts ───────────────────────────────────────────────────────────────
    r'^\s*artifacts?\s*$',           # just "artifacts" alone as the whole message
    r'\blist\s+artifacts?\b', r'\bshow\s+artifacts?\b',
    r'\bdownload\s+(?:the\s+)?(?:artifacts?|files?|dlls?|jars?)\b',
    r'\bget\s+(?:the\s+)?(?:artifacts?|files?|dlls?|jars?)\b',
    r'\bartifacts?\s+(?:from|for|of|in|at|location|path)\b',
    r'\bartifact\s+(?:location|path|dir(?:ectory)?|folder|url|address)\b',
    r'\bwhere\s+(?:are|is)\s+(?:the\s+)?artifacts?\b',
    r'\bneed(?:\s+only)?\s+artifact\b',  # "need only artifact location of csharp"
    r'\bjust\s+(?:the\s+)?artifact\b',   # "just artifact of X"
    r'\bonly\s+artifact\b',              # "only artifact location"
    r'\bbuilt\s+files?\b',
    r'\bshow\s+(?:me\s+)?(?:the\s+)?artifacts?\b',
    r'\bget\s+(?:me\s+)?(?:the\s+)?artifacts?\b',

    # ── Console / build log ─────────────────────────────────────────────────────
    r'\bconsole\s+logs?\b', r'\bbuild\s+logs?\b',
    r'\bshow\s+(?:the\s+)?(?:logs?|output|console)\b',
    r'\bget\s+(?:the\s+)?(?:logs?|output|console)\b',
    r'\bfetch\s+(?:the\s+)?(?:logs?|output|console)\b',
    r'\bprint\s+(?:the\s+)?(?:logs?|output|console)\b',
    r'\blogs?\s+(?:for|of|from)\b', r'\boutput\s+(?:for|of|from)\b',
    r'\blast\s+\d+\s+lines?\b',     # "last 20 lines"

    # ── SFTP upload paths ───────────────────────────────────────────────────────
    r'\bsftp\b', r'\bupload\s+path\b', r'\bwhere.*(?:uploaded|deployed)\b',
    r'\buploaded\s+(?:to|path|location)\b', r'\bscp\s+path\b',

    # ── Failure analysis ────────────────────────────────────────────────────────
    r'\bwhy.*failed\b', r'\bwhy.*fail\b', r'\bwhy\s+(?:did|is)\b',
    r'\banalyze.*fail\b', r'\banalyse.*fail\b',
    r'\bfailure\s+(?:reason|analysis|cause)\b', r'\broot\s+cause\b',
    r'\bwhat\s+(?:went\s+wrong|caused|broke)\b',
    r'\berror\s+(?:in|of|for|logs?)\b', r'\bbuild\s+error\b', r'\bjob\s+error\b',
    r'\blast\s+\d+\s+error\b',         # "last 20 error lines"
    r'\bcheck\s+(?:the\s+)?(?:error|failure|logs?)\b',

    # ── Build / job status ──────────────────────────────────────────────────────
    r'\bbuild\s+status\b', r'\bjob\s+status\b', r'\blast\s+build\b',
    r'\bstatus\s+of\b', r'\bstate\s+of\b',
    r'\bis\s+(?:the\s+)?(?:build|job)\b',    # "is the build done"
    r'\bcheck\s+(?:the\s+)?(?:build|job)\s+status\b',

    # ── Search jobs ─────────────────────────────────────────────────────────────
    r'\bsearch\s+(?:for\s+)?jobs?\b', r'\bfind\s+(?:a\s+)?jobs?\b',

    # ── Running builds (Group A) ─────────────────────────────────────────────────
    r'\brunning\s+builds?\b', r'\bwhat.*(?:is|are).*building\b',
    r'\bcurrently\s+building\b', r'\bactive\s+builds?\b',
    r'\bbuilds?\s+(?:in\s+progress|running|now)\b',
    r'\bwhat.*running\b', r'\bshow.*running\b',

    # ── Build history (Group A) ──────────────────────────────────────────────────
    r'\bbuild\s+history\b', r'\blast\s+\d+\s+builds?\b',
    r'\brecent\s+builds?\b', r'\bprevious\s+builds?\b', r'\bbuild\s+list\b',
    r'\bshow.*builds?\s+for\b', r'\blist.*builds?\s+of\b',

    # ── Retry / rebuild (Group A) ────────────────────────────────────────────────
    r'\bretry\s+(?:the\s+)?build\b', r'\brebuild\b',
    r'\brun\s+(?:it\s+)?again\b', r'\bretrigger\b', r'\brerun\b',

    # ── Jenkins queue (Group A) ──────────────────────────────────────────────────
    r'\bjenkins\s+queue\b', r'\bbuild\s+queue\b',
    r'\bpending\s+builds?\b', r'\bwaiting\s+(?:to\s+)?build\b',
    r'\bin\s+(?:the\s+)?queue\b', r'\bqueue\s+status\b',

    # ── Agents / nodes (Group A) ─────────────────────────────────────────────────
    r'\bagents?\s+(?:status|list|online|offline)\b',
    r'\bnodes?\s+(?:status|list|online|offline)\b',
    r'\bjenkins\s+(?:agents?|nodes?)\b',
    r'\bexecutors?\b',
    r'\bshow\s+(?:agents?|nodes?)\b', r'\blist\s+(?:agents?|nodes?)\b',

    # ── Build changes / commits (Group B) ────────────────────────────────────────
    r'\bwhat\s+changed\b', r'\bgit\s+(?:commits?|changes?|log)\b',
    r'\bchangeset\b', r'\bchange\s+log\b',
    r'\bcommits?\s+in\b', r'\bchanged\s+files?\b',
    r'\bwho\s+committed\b', r'\bscm\s+changes?\b',

    # ── Compare builds (Group B) ─────────────────────────────────────────────────
    r'\bcompare\s+(?:build|builds?)\b', r'\bdiff\s+(?:build|builds?)\b',
    r'\bbuild\s+\d+\s+(?:vs|versus|and)\s+(?:build\s+)?\d+\b',
    r'\bversus\b.*\bbuild\b',

    # ── Failed build history (Group B) ───────────────────────────────────────────
    r'\bfailed\s+builds?\b', r'\ball\s+failures\b',
    r'\bfailure\s+history\b', r'\bbuilds?\s+that\s+failed\b',
    r'\brecent\s+failures\b', r'\blist\s+failures\b',

    # ── Jenkins info / version (Group B) ─────────────────────────────────────────
    r'\bjenkins\s+version\b', r'\bjenkins\s+health\b',
    r'\bjenkins\s+info(?:rmation)?\b', r'\bjenkins\s+details?\b',

    # ── Plugins (Group B) ────────────────────────────────────────────────────────
    r'\bplugins?\b', r'\binstalled\s+plugins?\b', r'\bjenkins\s+plugins?\b',
    r'\blist\s+plugins?\b', r'\bshow\s+plugins?\b',
]
_QUERY_RE = re.compile("|".join(_QUERY_KEYWORDS), re.IGNORECASE)


def detect_intent(message: str) -> str:
    """
    Fast keyword-based intent classifier. No LLM call.

    Returns "build" if the message looks like a build/deploy request.
    Returns "chat"  for greetings, general questions, or anything unclear.

    "build" → proceed with Jenkins job discovery flow
    "chat"  → respond via general_chat_response(), skip Jenkins entirely
    """
    msg = message.strip()

    if _CHAT_RE.search(msg):
        return "chat"

    if _QUERY_RE.search(msg):
        return "query"

    if _BUILD_RE.search(msg):
        return "build"

    if len(msg.split()) <= 4:
        return "chat"

    return "build"


# ─── General chat response ────────────────────────────────────────────────────────

_GENERAL_SYSTEM = """You are BuildBot, a DevOps assistant inside a Jenkins build tool.
Help with build questions and general DevOps chat. Be concise (2-3 sentences max).
To trigger a build, users type: "build hotfix/PAY-1 from https://github.com/acme/repo"
Do NOT trigger Jenkins builds yourself."""


def general_chat_response(message: str) -> str:
    """
    Respond to a general (non-build) message using the LLM.
    Does NOT call Jenkins or extract parameters.

    Returns the assistant's plain-text reply string.
    Falls back to a simple default on any error.
    """
    model = os.getenv("LLM_MODEL", "")

    try:
        client   = get_client()    # ← inside try so client errors are caught
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _GENERAL_SYSTEM},
                {"role": "user",   "content": f"/no_think\n{message}"},
            ],
            temperature=0.7,
            max_tokens=100,
        )
        reply = (response.choices[0].message.content or "").strip()
        # Strip any /no_think artifacts
        reply = reply.replace("/no_think", "").strip()
        return reply or "Hi! I'm BuildBot. Tell me what to build and I'll take care of it."
    except Exception as exc:
        logger.warning("general_chat_response failed: %s", exc)
        return (
            "Hi! I'm BuildBot — your DevOps assistant. "
            "To trigger a build, tell me something like: "
            '"build hotfix/PAY-1 from https://github.com/acme/repo"'
        )


# ─── Jenkins query parser ────────────────────────────────────────────────────────

_QUERY_PARSE_SYSTEM = """You are BuildBot. Parse a Jenkins management request into a JSON action object.
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
- "retry/rebuild/run again/rerun build" → retry_build
- "show/get/fetch/print logs/output/console" → console_log
- "why did it fail / what went wrong / root cause / analyze failure" → analyze_failure
- "show/list/get artifacts/files/dlls" → list_artifacts
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
{"action":"...","job_name":null,"build_number":null,"view_name":null,"lines":50,"search_query":null,"build_number_b":null,"count":10}

Rules:
- job_name / view_name must be an exact name from the provided lists, or null.
- build_number: integer when user mentions a specific build number, else null.
- build_number_b: second build number for compare_builds (e.g. "compare 5 and 6").
- lines: defaults to 50 for console_log, 20 for analyze_failure.
- count: number of builds for list_build_history / search_failed_builds (default 10).
- search_query: only for search_jobs.
- If unclear, action = "unknown"."""


def parse_jenkins_query(
    message: str,
    job_names: list[str],
    view_names: list[str],
) -> dict:
    """
    Use the LLM to parse a Jenkins management request into a structured action.

    Returns:
    {
        "action": str,        # one of the action labels above
        "job_name": str|None,
        "build_number": int|None,
        "view_name": str|None,
        "lines": int,
        "search_query": str|None,
    }
    Never raises — returns {"action": "unknown", ...} on any failure.
    """
    model = os.getenv("LLM_MODEL", "")

    catalogue = (
        "Jobs: "  + (", ".join(job_names[:50]) or "none") + "\n"
        "Views: " + (", ".join(view_names)     or "none")
    )

    messages = [
        {"role": "system", "content": _QUERY_PARSE_SYSTEM + "\n\n" + catalogue},
        {"role": "user",   "content": f"/no_think\n{message}"},
    ]

    def _call() -> str:
        client   = get_client()
        response = client.chat.completions.create(
            model=model, messages=messages, temperature=0, max_tokens=80,
        )
        return response.choices[0].message.content or ""

    raw = ""
    try:
        raw    = _call()
        result = _extract_json(raw)
    except Exception as exc:
        logger.warning("parse_jenkins_query first attempt failed (%s) — retrying", exc)
        if raw:
            messages.append({"role": "assistant", "content": raw})
        messages.append({"role": "user", "content": "Return only the raw JSON object."})
        try:
            raw    = _call()
            result = _extract_json(raw)
        except Exception as exc2:
            logger.error("parse_jenkins_query failed twice: %s", exc2)
            return {
                "action": "unknown", "job_name": None, "build_number": None,
                "view_name": None, "lines": 50, "search_query": None,
            }

    # Validate job/view names against the known lists
    valid_jobs  = set(job_names)
    valid_views = set(view_names)
    job_name    = result.get("job_name")
    view_name   = result.get("view_name")
    if job_name  and job_name  not in valid_jobs:
        logger.warning("parse_jenkins_query returned unknown job %r — nulled", job_name)
        job_name = None
    if view_name and view_name not in valid_views:
        logger.warning("parse_jenkins_query returned unknown view %r — nulled", view_name)
        view_name = None

    try:
        lines = int(result.get("lines") or 50)
    except (TypeError, ValueError):
        lines = 50

    try:
        count = int(result.get("count") or 10)
        count = max(1, min(count, 50))   # clamp 1–50
    except (TypeError, ValueError):
        count = 10

    build_number_b = result.get("build_number_b")
    try:
        build_number_b = int(build_number_b) if build_number_b else None
    except (TypeError, ValueError):
        build_number_b = None

    return {
        "action":         result.get("action", "unknown"),
        "job_name":       job_name,
        "build_number":   result.get("build_number"),
        "build_number_b": build_number_b,
        "view_name":      view_name,
        "lines":          lines,
        "count":          count,
        "search_query":   result.get("search_query"),
    }


# ─── Job browser filter update parser ────────────────────────────────────────────

_JOB_FILTER_SYSTEM = """You are BuildBot. Parse a job browser filter command.
Current filters: {current_json}

Return ONLY JSON:
{"action":"filter|next|prev|page_N|clear|select","status":"","hotfix":false,"q":"","repo":"","branch":"","sort":"","select_job":null}

action values:
- "filter": apply/change filters
- "next": go to next page
- "prev": go to previous page
- "page_N": go to specific page (replace N with the number)
- "clear": reset all filters
- "select": user named a specific job (set select_job to the job name)

status values: FAILURE|SUCCESS|BUILDING|UNSTABLE|ALL (empty = no change)
sort values: name|status|failed|duration (empty = no change)
Leave fields empty/null/false if not mentioned."""


def parse_job_filter_update(message: str, current_filters: dict) -> dict:
    """
    Parse a conversational filter update for the job browser.
    Returns action dict with filter changes. Never raises.
    """
    import json as _json
    model = os.getenv("LLM_MODEL", "")

    system = _JOB_FILTER_SYSTEM.replace(
        "{current_json}", _json.dumps(current_filters, ensure_ascii=False)
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user",   "content": f"/no_think\n{message}"},
    ]

    def _call() -> str:
        client   = get_client()
        response = client.chat.completions.create(
            model=model, messages=messages, temperature=0, max_tokens=80,
        )
        return response.choices[0].message.content or ""

    raw = ""
    try:
        raw    = _call()
        result = _extract_json(raw)
    except Exception as exc:
        logger.warning("parse_job_filter_update failed (%s) — regex fallback", exc)
        msg_lower = message.lower()
        if any(w in msg_lower for w in ("next", "more", "forward")):
            return {"action": "next"}
        if any(w in msg_lower for w in ("prev", "back", "before")):
            return {"action": "prev"}
        if any(w in msg_lower for w in ("clear", "reset", "all jobs")):
            return {"action": "clear"}
        if any(w in msg_lower for w in ("fail", "broken", "error")):
            return {"action": "filter", "status": "FAILURE"}
        if re.search(r'\b(hotfix|hf)\b', message, re.IGNORECASE):
            return {"action": "filter", "hotfix": True}
        return {"action": "filter"}

    return {
        "action":     result.get("action", "filter"),
        "status":     result.get("status", ""),
        "hotfix":     bool(result.get("hotfix", False)),
        "q":          result.get("q", ""),
        "repo":       result.get("repo", ""),
        "branch":     result.get("branch", ""),
        "sort":       result.get("sort", ""),
        "select_job": result.get("select_job"),
    }


# ─── LLM-powered build failure analysis ──────────────────────────────────────────

_FAILURE_SYSTEM = """You are a DevOps expert. Analyse why a Jenkins build failed.
Respond ONLY with a JSON object:
{"cause": "<root cause, max 15 words>", "suggestion": "<fix, max 25 words>", "severity": "error"|"warning"|"info"}
Be specific — name the exact file or command that failed. Raw JSON only, no prose."""


def llm_analyze_build_failure(console_lines: list[str], result: str) -> dict:
    """
    Use the LLM to analyse Jenkins console output and explain why a build failed.

    Parameters
    ----------
    console_lines : Last N lines of console output from Jenkins
    result        : "SUCCESS" | "FAILURE" | "ABORTED" | "UNSTABLE"

    Returns
    -------
    {"cause": str, "suggestion": str, "severity": "error"|"warning"|"info", "source": "llm"|"regex"}

    Falls back to regex-based analyze_console_failure() on any LLM error.
    """
    from services.jenkins_client import analyze_console_failure as _regex_analyze

    if not console_lines:
        return {"cause": "No console output available.", "suggestion": "Check Jenkins job configuration.", "severity": "error", "source": "fallback"}

    # Only send to LLM if the build actually failed/was unstable
    if result == "SUCCESS":
        return {"cause": "", "suggestion": "", "severity": "info", "source": "skip"}

    console_text = "\n".join(console_lines[-50:])   # last 50 lines
    model = os.getenv("LLM_MODEL", "")

    try:
        client = get_client()
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _FAILURE_SYSTEM},
                {"role": "user",   "content": f"/no_think\nBuild result: {result}\n\nConsole output:\n{console_text}"},
            ],
            temperature=0,
            max_tokens=120,
            timeout=20.0,   # hard cap: don't block the build result card for > 20s
        )
        raw = (response.choices[0].message.content or "").strip()
        parsed = _extract_json(raw)
        return {
            "cause":      parsed.get("cause", ""),
            "suggestion": parsed.get("suggestion", ""),
            "severity":   parsed.get("severity", "error"),
            "source":     "llm",
        }
    except Exception as exc:
        logger.warning("LLM build failure analysis failed (%s) — falling back to regex", exc)
        # Fallback: use regex patterns
        regex_result = _regex_analyze(console_lines)
        regex_result["source"] = "regex"
        return regex_result

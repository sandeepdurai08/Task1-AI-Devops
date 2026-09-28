"""
services/jenkins_client.py
──────────────────────────
All Jenkins REST API interactions for BuildBot.

Public API
──────────
    list_jobs()                            -> list[dict]   (name, description, status)
    get_job_parameters(job_name)           -> list[dict]
    trigger_build(job_name, params)        -> queue_url (str)
    poll_queue(queue_url)                  -> build_number (int)
    poll_build(job_name, build_number)     -> BuildResult (dict)

Jenkins REST quirks handled here:
  - POST buildWithParameters returns an empty body; useful info is in the
    Location response header (a queue-item URL, not a build URL).
  - The queue item must be polled until an "executable" object appears.
  - Auth must use API token via HTTP Basic, NOT a password — avoids CSRF crumb issues.
  - Boolean params are sent as lowercase strings "true"/"false".
  - Multi-value choice params are sent as comma-joined strings.
"""

import logging
import os
import re
import time
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests
from requests.auth import HTTPBasicAuth
from requests.exceptions import ConnectionError as ReqConnError, Timeout

logger = logging.getLogger(__name__)

# ─── In-memory caches ────────────────────────────────────────────────────────────
import threading as _threading

_jobs_cache_lock   = _threading.Lock()
_jobs_cache: dict  = {"data": None, "ts": 0.0}   # populated by list_jobs()
JOBS_CACHE_TTL_S   = 30.0   # seconds — job list is stable; refresh every 30s

_params_cache: dict[str, tuple] = {}   # job_name -> (param_defs, monotonic_ts)
PARAMS_CACHE_TTL_S = 300.0  # 5 minutes — job schemas rarely change

# ─── Timeouts & intervals ────────────────────────────────────────────────────────
QUEUE_POLL_INTERVAL_S  = 2      # seconds between queue item polls (was 3)
QUEUE_TIMEOUT_S        = 120
BUILD_POLL_INTERVAL_S  = 3      # seconds between build status polls (was 5)
BUILD_TIMEOUT_S        = 1800
HTTP_TIMEOUT_S         = 15


# ─── Exceptions ──────────────────────────────────────────────────────────────────

class JenkinsError(Exception):
    """Base class for all Jenkins client errors."""


class JenkinsDownError(JenkinsError):
    """Jenkins is unreachable."""


class JenkinsJobNotFoundError(JenkinsError):
    """The requested job does not exist."""


class JenkinsBuildTimeoutError(JenkinsError):
    """Polling timed out waiting for a build to finish."""


class JenkinsQueueTimeoutError(JenkinsError):
    """Polling timed out waiting for a queued item to become a build."""


# ─── Auth helper ─────────────────────────────────────────────────────────────────

def _auth() -> HTTPBasicAuth:
    """Return HTTP Basic auth using JENKINS_USER + JENKINS_TOKEN (API token, not password)."""
    user  = os.getenv("JENKINS_USER", "")
    token = os.getenv("JENKINS_TOKEN", "")
    if not user or not token:
        raise JenkinsError(
            "JENKINS_USER or JENKINS_TOKEN is not set in .env. "
            "Generate an API token at: Jenkins → Users → <your user> → Security."
        )
    return HTTPBasicAuth(user, token)


def _base_url() -> str:
    return os.getenv("JENKINS_URL", "http://localhost:8080").rstrip("/")


# ─── Low-level GET / POST ─────────────────────────────────────────────────────────

def _get(url: str, user_auth: HTTPBasicAuth | None = None, **kwargs) -> requests.Response:
    """GET with auth and standard error handling.
    user_auth overrides the service account when provided (per-user Jenkins credentials).
    """
    auth = user_auth or _auth()
    try:
        resp = requests.get(url, auth=auth, timeout=HTTP_TIMEOUT_S, **kwargs)
    except (ReqConnError, Timeout) as exc:
        raise JenkinsDownError(f"Cannot reach Jenkins at {url}: {exc}") from exc

    if resp.status_code == 404:
        raise JenkinsJobNotFoundError(f"Jenkins returned 404 for {url}")
    if resp.status_code == 403:
        raise JenkinsError(
            f"Jenkins returned 403 (Forbidden) for {url}. "
            "Make sure you are using an API token, not a password."
        )
    resp.raise_for_status()
    return resp


def _post(url: str, user_auth: HTTPBasicAuth | None = None, **kwargs) -> requests.Response:
    """POST with auth and standard error handling.
    user_auth overrides the service account when provided (per-user Jenkins credentials).
    """
    auth = user_auth or _auth()
    try:
        resp = requests.post(url, auth=auth, timeout=HTTP_TIMEOUT_S, **kwargs)
    except (ReqConnError, Timeout) as exc:
        raise JenkinsDownError(f"Cannot reach Jenkins at {url}: {exc}") from exc

    if resp.status_code == 404:
        raise JenkinsJobNotFoundError(f"Jenkins returned 404 for {url}")
    if resp.status_code == 403:
        msg = (
            f"Jenkins returned 403 (Forbidden) for {url}. "
        )
        if user_auth:
            msg += (
                "Your Jenkins account does not have Build permission for this job. "
                "Ask your Jenkins admin to grant you access."
            )
        else:
            msg += "Make sure the service account has 'Build' permission on this job."
        raise JenkinsError(msg)
    # 201 = build queued successfully; anything 2xx is fine
    if not resp.ok and resp.status_code != 201:
        raise JenkinsError(f"Jenkins POST {url} returned {resp.status_code}: {resp.text[:200]}")
    return resp


# ─── 1. Get job parameter schema ─────────────────────────────────────────────────

def get_job_parameters(job_name: str) -> list[dict]:
    """
    Fetch the parameter definitions for a Jenkins job.

    Returns a list of dicts, one per parameter:
        {
            "name":        "GITHUB_URL",
            "type":        "StringParameterDefinition",   # or Boolean… or Choice…
            "default":     "https://...",                 # may be None
            "choices":     ["A", "B", "C"],               # only for Choice params
            "description": "Repository URL to build",
        }

    Raises JenkinsJobNotFoundError, JenkinsDownError, or JenkinsError.
    """
    # ── Cache check ──────────────────────────────────────────────────────────────
    cached = _params_cache.get(job_name)
    if cached and time.monotonic() - cached[1] < PARAMS_CACHE_TTL_S:
        logger.debug("get_job_parameters(%r) → cache hit", job_name)
        return cached[0]

    tree = "property[parameterDefinitions[name,type,defaultParameterValue[value],description,choices]]"
    from urllib.parse import quote
    url  = f"{_base_url()}/job/{quote(job_name, safe='')}/api/json"
    resp = _get(url, params={"tree": tree})
    data = resp.json()

    param_defs = []
    for prop in data.get("property", []):
        for pd in prop.get("parameterDefinitions", []):
            raw_default = pd.get("defaultParameterValue") or {}
            entry = {
                "name":        pd.get("name", ""),
                "type":        pd.get("type", "StringParameterDefinition"),
                "default":     raw_default.get("value"),   # None if not set
                "choices":     pd.get("choices", []),
                "description": pd.get("description", ""),
            }
            # Clean up: BooleanParameterDefinition default is a string "true"/"false"
            if entry["type"] == "BooleanParameterDefinition" and isinstance(entry["default"], str):
                entry["default"] = entry["default"].lower() == "true"

            param_defs.append(entry)

    logger.debug("get_job_parameters(%r) → %d params", job_name, len(param_defs))
    _params_cache[job_name] = (param_defs, time.monotonic())   # store in cache
    return param_defs


# ─── 2. Trigger a build ──────────────────────────────────────────────────────────

def _serialise_params(params: dict) -> dict:
    """
    Convert Python values to Jenkins-friendly query-string values.
      bool  → "true" / "false"
      list  → comma-joined string  (for multi-select choice params)
      other → str(value)
    """
    out = {}
    for k, v in params.items():
        if isinstance(v, bool):
            out[k] = "true" if v else "false"
        elif isinstance(v, list):
            out[k] = ",".join(str(i) for i in v)
        else:
            out[k] = str(v) if v is not None else ""
    return out


def trigger_build(job_name: str, params: dict, user_auth: HTTPBasicAuth | None = None) -> str:
    """..."""
    from urllib.parse import quote
    url           = f"{_base_url()}/job/{quote(job_name, safe='')}/buildWithParameters"
    serial_params = _serialise_params(params)

    logger.info("Triggering Jenkins job %r (user_auth=%s) with params: %s",
                job_name, "user" if user_auth else "service-account", serial_params)
    resp = _post(url, user_auth=user_auth, params=serial_params)

    # Jenkins returns 201 with a Location header pointing to the queue item.
    location = resp.headers.get("Location", "")
    if not location:
        raise JenkinsError(
            "Jenkins did not return a Location header after triggering the build. "
            "Check Jenkins logs for details."
        )

    # Ensure the queue URL has a trailing slash (required for the /api/json sub-path).
    queue_url = location.rstrip("/") + "/"
    logger.info("Build queued → queue URL: %s", queue_url)
    return queue_url


# ─── 3. Poll queue item → build number ──────────────────────────────────────────

def poll_queue(queue_url: str) -> int:
    """
    Poll the Jenkins queue item until it becomes an actual build.

    Returns the build number (int) once the build has started.
    Raises JenkinsQueueTimeoutError after QUEUE_TIMEOUT_S seconds.
    """
    api_url  = queue_url.rstrip("/") + "/api/json"
    deadline = time.monotonic() + QUEUE_TIMEOUT_S

    logger.info("Polling queue item: %s", api_url)
    while time.monotonic() < deadline:
        resp = _get(api_url)
        data = resp.json()

        # "cancelled" — someone cancelled the queue item
        if data.get("cancelled"):
            raise JenkinsError("Build was cancelled in the queue before it started.")

        # "executable" appears once Jenkins starts the build
        executable = data.get("executable")
        if executable and executable.get("number"):
            build_number = int(executable["number"])
            logger.info("Build started → build number: %d", build_number)
            return build_number

        logger.debug("Queue item not yet executable, waiting %ds…", QUEUE_POLL_INTERVAL_S)
        time.sleep(QUEUE_POLL_INTERVAL_S)

    raise JenkinsQueueTimeoutError(
        f"Timed out after {QUEUE_TIMEOUT_S}s waiting for build to leave the queue. "
        "Check Jenkins executor availability."
    )


# ─── 4. Poll build status ────────────────────────────────────────────────────────

def _format_duration(duration_ms: int) -> str:
    """Convert milliseconds to a human-readable '2m 14s' string."""
    if duration_ms <= 0:
        return "0s"
    total_s = duration_ms // 1000
    minutes = total_s // 60
    seconds = total_s % 60
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def poll_build(job_name: str, build_number: int) -> dict:
    """
    Poll a Jenkins build until it completes.

    Returns
    -------
    {
        "result":       "SUCCESS" | "FAILURE" | "ABORTED" | "UNSTABLE",
        "duration_ms":  134000,
        "duration_str": "2m 14s",
        "url":          "http://localhost:8080/job/hotfix-build/143/",
        "console_tail": ["last", "20", "lines", ...],   # always populated
    }

    Raises JenkinsBuildTimeoutError after BUILD_TIMEOUT_S seconds.
    """
    api_url  = f"{_base_url()}/job/{job_name}/{build_number}/api/json"
    deadline = time.monotonic() + BUILD_TIMEOUT_S

    logger.info("Polling build %s #%d", job_name, build_number)
    while time.monotonic() < deadline:
        resp = _get(api_url)
        data = resp.json()

        building = data.get("building", True)
        result   = data.get("result")   # None while building

        if not building and result is not None:
            duration_ms  = data.get("duration", 0)
            build_url    = data.get("url", f"{_base_url()}/job/{job_name}/{build_number}/")
            console_tail = _fetch_console_tail(job_name, build_number, lines=20)

            logger.info("Build %s #%d finished: %s (%s)",
                        job_name, build_number, result, _format_duration(duration_ms))
            return {
                "result":       result,
                "duration_ms":  duration_ms,
                "duration_str": _format_duration(duration_ms),
                "url":          build_url,
                "console_tail": console_tail,
            }

        logger.debug("Build #%d still running, waiting %ds…", build_number, BUILD_POLL_INTERVAL_S)
        time.sleep(BUILD_POLL_INTERVAL_S)

    raise JenkinsBuildTimeoutError(
        f"Build #{build_number} did not complete within {BUILD_TIMEOUT_S // 60} minutes."
    )


# ─── Console log helper ──────────────────────────────────────────────────────────

def _fetch_console_tail(job_name: str, build_number: int, lines: int = 50) -> list[str]:
    """Fetch the last `lines` lines of a build's console log (increased to 50 for better LLM analysis)."""
    url = f"{_base_url()}/job/{job_name}/{build_number}/consoleText"
    try:
        resp = _get(url)
        all_lines = resp.text.splitlines()
        return all_lines[-lines:] if len(all_lines) > lines else all_lines
    except Exception as exc:
        logger.warning("Could not fetch console log for build #%d: %s", build_number, exc)
        return []


# ─── Connectivity check ──────────────────────────────────────────────────────────

def check_jenkins_alive() -> bool:
    """
    Quick liveness check — returns True if Jenkins responds, False otherwise.
    Does NOT raise; safe to call at startup.
    Logs a specific warning if credentials are missing rather than silently returning False.
    """
    try:
        resp = requests.get(
            f"{_base_url()}/api/json",
            auth=_auth(),          # may raise JenkinsError if creds are missing
            timeout=5,
        )
        return resp.ok
    except JenkinsError as exc:
        # Credential / config problem — surface it clearly rather than pretending Jenkins is down
        logger.error("Jenkins credentials error: %s", exc)
        return False
    except Exception:
        return False


# ─── Job discovery ───────────────────────────────────────────────────────────────

# Jenkins "color" field maps to build status
_COLOR_TO_STATUS = {
    "blue":          "last build: SUCCESS",
    "blue_anime":    "building",
    "red":           "last build: FAILED",
    "red_anime":     "building",
    "yellow":        "last build: UNSTABLE",
    "yellow_anime":  "building",
    "grey":          "never built",
    "grey_anime":    "building",
    "disabled":      "disabled",
    "aborted":       "last build: ABORTED",
    "notbuilt":      "never built",
}


def list_jobs() -> list[dict]:
    """
    Return all Jenkins jobs visible to the configured user, optionally filtered
    by the JENKINS_JOBS allowlist in .env.

    Each entry:
        {
            "name":        "hotfix-build",
            "description": "Builds hotfix branches for the Payments service",
            "status":      "last build: SUCCESS",   # human-readable
            "url":         "http://localhost:8080/job/hotfix-build/",
        }

    JENKINS_JOBS allowlist (comma-separated in .env):
        - If set:  only jobs whose name appears in the list are returned.
        - If empty: all jobs visible to JENKINS_USER are returned.

    Raises JenkinsDownError, JenkinsError on failure.
    """
    # ── Cache check ──────────────────────────────────────────────────────────────
    with _jobs_cache_lock:
        if _jobs_cache["data"] is not None and time.monotonic() - _jobs_cache["ts"] < JOBS_CACHE_TTL_S:
            logger.debug("list_jobs() → cache hit (%d jobs)", len(_jobs_cache["data"]))
            return _jobs_cache["data"]

    url  = f"{_base_url()}/api/json"
    tree = "jobs[name,description,color,url,lastBuild[number,result,duration,timestamp]]"
    resp = _get(url, params={"tree": tree})
    raw_jobs = resp.json().get("jobs", [])

    # Build allowlist from env (empty = allow all)
    allowlist_raw = os.getenv("JENKINS_JOBS", "").strip()
    allowlist = (
        {j.strip() for j in allowlist_raw.split(",") if j.strip()}
        if allowlist_raw
        else set()
    )

    jobs = []
    for j in raw_jobs:
        name = j.get("name", "")
        if allowlist and name not in allowlist:
            continue  # filtered out by allowlist

        color  = j.get("color", "grey")
        status = _COLOR_TO_STATUS.get(color, color)

        lb = j.get("lastBuild") or {}
        jobs.append({
            "name":        name,
            "description": (j.get("description") or "").strip(),
            "status":      status,
            "url":         j.get("url", ""),
            "last_build":  {
                "number":      lb.get("number"),
                "result":      lb.get("result"),
                "duration_ms": lb.get("duration", 0),
                "timestamp_s": (lb.get("timestamp") or 0) // 1000,
            } if lb else None,
        })

    logger.debug("list_jobs() → %d jobs (allowlist=%s)", len(jobs), allowlist or "all")
    with _jobs_cache_lock:
        _jobs_cache["data"] = jobs
        _jobs_cache["ts"]   = time.monotonic()
    return jobs


# ─── Failure diagnosis ───────────────────────────────────────────────────────────

# Each entry: (list_of_patterns, cause, suggestion)
_FAILURE_PATTERNS: list[tuple[list[str], str, str]] = [
    (
        ["couldn't find remote ref", "did not match any file(s) known to git",
         "pathspec.*did not match", "remote: repository not found",
         "fatal: not a git repository"],
        "Branch or repository not found",
        "Check the branch name and repo URL — does the branch exist in the remote?",
    ),
    (
        ["compilation failure", "compile error", "error: cannot find symbol",
         "build failure\nbuild failure", "javac: error", "cs(s): error",
         "msbuild : error", ": error cs"],
        "Compilation error",
        "Check the console output for the failing file and line number.",
    ),
    (
        ["tests run:", "test.*failed", "testcase.*failure", "assertion.*failed",
         "assertionerror", "testfailure"],
        "Test failures",
        "Run the tests locally first to identify the failing test case.",
    ),
    (
        ["java.lang.outofmemoryerror", "out of memory", "gc overhead limit exceeded",
         "cannot allocate memory"],
        "Build ran out of memory",
        "Increase Jenkins executor memory (-Xmx) or reduce build scope.",
    ),
    (
        ["connection refused", "unable to connect", "connection timed out",
         "could not resolve host", "network is unreachable"],
        "Network or dependency error",
        "Check if the artifact repository, package feed, or remote service is reachable.",
    ),
    (
        ["permission denied", "access is denied", "access denied",
         "unauthorized", "403 forbidden"],
        "Permission or authentication error",
        "Check file/directory permissions in the Jenkins workspace and credentials.",
    ),
    (
        ["no space left on device", "disk full", "not enough space"],
        "Disk space exhausted on the Jenkins executor",
        "Free up disk space on the Jenkins build agent.",
    ),
    (
        ["timeout", "timed out", "build timed out"],
        "Build timed out",
        "The build exceeded its time limit — check for hanging processes or slow steps.",
    ),
]


def analyze_console_failure(console_lines: list[str]) -> dict:
    """
    Scan the last N console lines to infer the root cause of a build failure.

    Returns
    -------
    {
        "cause":      "Branch or repository not found",   # short human-readable label
        "suggestion": "Check the branch name...",         # actionable tip
        "matched":    True | False,                       # whether a pattern was found
    }
    """
    if not console_lines:
        return {
            "cause":      "Build failed — no console output available",
            "suggestion": "Check the Jenkins job configuration.",
            "matched":    False,
        }

    # Join and lower-case once for fast matching
    text = "\n".join(console_lines).lower()

    for patterns, cause, suggestion in _FAILURE_PATTERNS:
        for pat in patterns:
            if re.search(pat, text):
                logger.debug("Failure pattern matched: %r → %s", pat, cause)
                return {"cause": cause, "suggestion": suggestion, "matched": True}

    return {
        "cause":      "Build failed",
        "suggestion": "Review the console output below for the root cause.",
        "matched":    False,
    }


# ═══════════════════════════════════════════════════════════════════════════════
#  EXTENDED QUERY API — views, build management, artifacts, console, permissions
# ═══════════════════════════════════════════════════════════════════════════════

# ─── Views ───────────────────────────────────────────────────────────────────────

def list_views() -> list[dict]:
    """
    Return all Jenkins views (dashboard tabs / folders).
    Each entry: {name, url, job_count, job_names: [str]}
    """
    url  = f"{_base_url()}/api/json"
    tree = "views[name,url,jobs[name]]"
    resp = _get(url, params={"tree": tree})
    raw  = resp.json().get("views", [])
    views = []
    for v in raw:
        job_names = [j.get("name", "") for j in v.get("jobs", [])]
        views.append({
            "name":      v.get("name", ""),
            "url":       v.get("url", ""),
            "job_count": len(job_names),
            "job_names": job_names,
        })
    logger.debug("list_views() → %d views", len(views))
    return views


def list_jobs_in_view(view_name: str) -> list[dict]:
    """
    Return jobs inside a specific Jenkins view.
    Same shape as list_jobs().
    Raises JenkinsJobNotFoundError if the view does not exist.
    """
    from urllib.parse import quote
    url  = f"{_base_url()}/view/{quote(view_name, safe='')}/api/json"
    tree = "jobs[name,description,color,url]"
    resp = _get(url, params={"tree": tree})
    raw_jobs = resp.json().get("jobs", [])
    jobs = []
    for j in raw_jobs:
        color  = j.get("color", "grey")
        status = _COLOR_TO_STATUS.get(color, color)
        jobs.append({
            "name":        j.get("name", ""),
            "description": (j.get("description") or "").strip(),
            "status":      status,
            "url":         j.get("url", ""),
        })
    logger.debug("list_jobs_in_view(%r) → %d jobs", view_name, len(jobs))
    return jobs


# ─── Build management ────────────────────────────────────────────────────────────

def abort_build(job_name: str, build_number: int) -> bool:
    """
    Stop a running build. Tries /stop first, then /kill.
    Returns True if accepted (200/302/303).
    """
    from urllib.parse import quote
    base = f"{_base_url()}/job/{quote(job_name, safe='')}/{build_number}"
    for endpoint in ("/stop", "/kill"):
        try:
            resp = _post(f"{base}{endpoint}")
            if resp.status_code in (200, 302, 303):
                logger.info("abort_build %s #%d via %s", job_name, build_number, endpoint)
                return True
        except Exception:
            pass
    logger.warning("abort_build failed for %s #%d", job_name, build_number)
    return False


def get_build_trigger(job_name: str, build_number: Optional[int] = None) -> dict:
    """
    Return who/what triggered a build.
    Uses lastBuild when build_number is None.
    Returns: {triggered_by, cause, build_number, url}
    """
    from urllib.parse import quote
    ref  = str(build_number) if build_number else "lastBuild"
    url  = f"{_base_url()}/job/{quote(job_name, safe='')}/{ref}/api/json"
    tree = "number,url,actions[causes[userId,userName,shortDescription]]"
    resp = _get(url, params={"tree": tree})
    data = resp.json()

    triggered_by = "unknown"
    cause        = ""
    for action in data.get("actions", []):
        for c in action.get("causes", []):
            cause = c.get("shortDescription", "")
            uid   = c.get("userId") or c.get("userName", "")
            if uid:
                triggered_by = uid
                break
        if triggered_by != "unknown":
            break

    return {
        "triggered_by": triggered_by,
        "cause":        cause,
        "build_number": data.get("number"),
        "url":          data.get("url", ""),
    }


def get_last_build_number(job_name: str) -> Optional[int]:
    """Return the build number of the most recent build, or None if never built."""
    from urllib.parse import quote
    url = f"{_base_url()}/job/{quote(job_name, safe='')}/api/json"
    try:
        resp = _get(url, params={"tree": "lastBuild[number]"})
        lb   = resp.json().get("lastBuild")
        return int(lb["number"]) if lb else None
    except Exception:
        return None


# ─── Artifacts ───────────────────────────────────────────────────────────────────

def list_build_artifacts(
    job_name: str,
    build_number: Optional[int] = None,
) -> dict:
    """
    List artifacts from a build.
    Uses lastSuccessfulBuild when build_number is None.
    Returns: {build_number, artifacts: [{name, relative_path, download_url}]}
    """
    from urllib.parse import quote
    ref  = str(build_number) if build_number else "lastSuccessfulBuild"
    url  = f"{_base_url()}/job/{quote(job_name, safe='')}/{ref}/api/json"
    tree = "number,artifacts[fileName,relativePath]"
    resp = _get(url, params={"tree": tree})
    data = resp.json()

    actual_num = data.get("number", build_number)
    artifacts  = []
    for art in data.get("artifacts", []):
        fname    = art.get("fileName", "")
        rel_path = art.get("relativePath", "")
        dl_url   = (
            f"{_base_url()}/job/{quote(job_name, safe='')}"
            f"/{actual_num}/artifact/{rel_path}"
        )
        artifacts.append({
            "name":          fname,
            "relative_path": rel_path,
            "download_url":  dl_url,
        })

    logger.debug("list_build_artifacts(%r, #%s) → %d files", job_name, ref, len(artifacts))
    return {"build_number": actual_num, "artifacts": artifacts}


# ─── Console log ─────────────────────────────────────────────────────────────────

def get_console_log_tail(
    job_name: str,
    build_number: Optional[int] = None,
    lines: int = 50,
) -> dict:
    """
    Fetch the last N lines of a build's console output.
    Uses lastBuild when build_number is None.
    Returns: {build_number, lines: [str], total_lines: int, result: str|None}
    """
    from urllib.parse import quote
    ref = str(build_number) if build_number else "lastBuild"

    # Resolve actual build number and result status
    info_url = f"{_base_url()}/job/{quote(job_name, safe='')}/{ref}/api/json"
    actual_num   = build_number
    build_result = None
    try:
        info = _get(info_url, params={"tree": "number,result"}).json()
        actual_num   = info.get("number", build_number)
        build_result = info.get("result")
    except Exception:
        pass

    log_url = f"{_base_url()}/job/{quote(job_name, safe='')}/{ref}/consoleText"
    try:
        resp      = _get(log_url)
        all_lines = resp.text.splitlines()
        tail      = all_lines[-lines:] if len(all_lines) > lines else all_lines
        return {
            "build_number": actual_num,
            "lines":        tail,
            "total_lines":  len(all_lines),
            "result":       build_result,
        }
    except Exception as exc:
        logger.warning("get_console_log_tail(%r, %s): %s", job_name, ref, exc)
        return {"build_number": actual_num, "lines": [], "total_lines": 0, "result": build_result}


def find_sftp_uploads(console_lines: list[str]) -> list[str]:
    """
    Scan console lines for SFTP / SCP / FTP upload destinations.
    Returns a deduplicated list of paths/destinations found.
    """
    _SFTP_PATTERNS = [
        r'(?i)uploading[:\s]+([/\w.\-_@: ]+)',
        r'(?i)sftp[>\s]+put\s+\S+\s+(\S+)',
        r'(?i)uploaded[:\s]+(?:to\s+)?([/\w.\-_@:]+)',
        r'(?i)transfer(?:red|ring)[:\s]+(?:to\s+)?([/\w.\-_@:]+)',
        r'(?i)remote[:\s]+path[:\s]+([/\w.\-_@:]+)',
        r'(?i)destination[:\s]+([/\w.\-_@:]+)',
        r'(?i)scp\b.*?(\S+@\S+:[/\w.\-_]+)',
        r'(?i)sending\s+file[:\s]+(?:\S+\s+[=>\-]+\s+)?([/\w.\-_@:]+)',
        r'(?i)ftp.*?to[:\s]+([/\w.\-_@:]+)',
    ]
    seen  = set()
    found = []
    for line in console_lines:
        for pat in _SFTP_PATTERNS:
            m = re.search(pat, line)
            if m:
                path = m.group(1).strip().rstrip("/.,;")
                if path and path not in seen and len(path) > 2:
                    seen.add(path)
                    found.append(path)
    return found


# ─── User / permissions ───────────────────────────────────────────────────────────

def get_current_user_info() -> dict:
    """
    Return the authenticated user's identity and granted authorities.
    Returns: {username, display_name, authorities: [str]}
    """
    url  = f"{_base_url()}/me/api/json"
    tree = "id,fullName,authorities"
    try:
        resp = _get(url, params={"tree": tree})
        data = resp.json()
        return {
            "username":     data.get("id", ""),
            "display_name": data.get("fullName", ""),
            "authorities":  data.get("authorities", []),
        }
    except Exception as exc:
        logger.warning("get_current_user_info failed: %s", exc)
        return {"username": "", "display_name": "", "authorities": []}


# ─── Job search ──────────────────────────────────────────────────────────────────

def search_jobs(query: str, jobs: Optional[list[dict]] = None) -> list[dict]:
    """
    Case-insensitive substring search over job name, description, and URL.
    Calls list_jobs() (cached) if jobs list not supplied.
    Name matches rank first; description/url matches rank second.
    """
    if jobs is None:
        jobs = list_jobs()

    q_terms = query.lower().split()
    exact, fuzzy = [], []

    for job in jobs:
        name_lower = job.get("name", "").lower()
        full_text  = (
            name_lower + " " +
            job.get("description", "").lower() + " " +
            job.get("url", "").lower()
        )
        if all(t in name_lower for t in q_terms):
            exact.append(job)
        elif all(t in full_text for t in q_terms):
            fuzzy.append(job)

    return exact + fuzzy


# ═══════════════════════════════════════════════════════════════════════════════
#  GROUP A — Build status, queue, agents
# ═══════════════════════════════════════════════════════════════════════════════

def list_running_builds() -> list[dict]:
    """
    Return all builds currently running across every visible job.
    Each entry: {job_name, build_number, url, elapsed_s}
    """
    url  = f"{_base_url()}/api/json"
    tree = "jobs[name,url,builds[number,building,url,timestamp,estimatedDuration]]"
    resp = _get(url, params={"tree": tree})
    now_ms   = time.time() * 1000
    running  = []
    for job in resp.json().get("jobs", []):
        for b in job.get("builds", []):
            if b.get("building"):
                elapsed_s = int((now_ms - b.get("timestamp", now_ms)) / 1000)
                running.append({
                    "job_name":     job.get("name", ""),
                    "build_number": b.get("number"),
                    "url":          b.get("url", ""),
                    "elapsed_s":    max(0, elapsed_s),
                })
    logger.debug("list_running_builds() → %d running", len(running))
    return running


def list_build_history(job_name: str, count: int = 10) -> list[dict]:
    """
    Return last `count` builds for a job with result, duration, and timestamp.
    Each entry: {number, result, duration_s, timestamp_s, url}
    """
    from urllib.parse import quote
    url  = f"{_base_url()}/job/{quote(job_name, safe='')}/api/json"
    tree = f"builds[number,result,duration,timestamp,url,building]{{0,{count}}}"
    resp = _get(url, params={"tree": tree})
    builds = []
    for b in resp.json().get("builds", []):
        result = b.get("result") or ("BUILDING" if b.get("building") else "UNKNOWN")
        builds.append({
            "number":      b.get("number"),
            "result":      result,
            "duration_s":  b.get("duration", 0) // 1000,
            "timestamp_s": b.get("timestamp", 0) // 1000,
            "url":         b.get("url", ""),
        })
    logger.debug("list_build_history(%r, %d) → %d builds", job_name, count, len(builds))
    return builds


def retry_build(job_name: str, build_number: int) -> bool:
    """
    Retry (rebuild) a specific build using the Rebuild plugin endpoint.
    Returns True if the rebuild was accepted.
    """
    from urllib.parse import quote
    url = f"{_base_url()}/job/{quote(job_name, safe='')}/{build_number}/rebuild"
    try:
        resp = _post(url)
        ok = resp.status_code in (200, 201, 302, 303)
        if ok:
            logger.info("retry_build %s #%d accepted", job_name, build_number)
        return ok
    except Exception as exc:
        logger.warning("retry_build %s #%d failed: %s", job_name, build_number, exc)
        return False


def list_queue() -> list[dict]:
    """
    Return all items currently waiting in the Jenkins build queue.
    Each entry: {job_name, url, why, blocked, stuck, in_queue_s}
    """
    url  = f"{_base_url()}/queue/api/json"
    tree = "items[id,why,blocked,buildable,stuck,task[name,url],inQueueSince]"
    resp = _get(url, params={"tree": tree})
    now_ms = time.time() * 1000
    items  = []
    for item in resp.json().get("items", []):
        task       = item.get("task", {})
        in_queue_s = int((now_ms - item.get("inQueueSince", now_ms)) / 1000)
        items.append({
            "id":         item.get("id"),
            "job_name":   task.get("name", ""),
            "url":        task.get("url", ""),
            "why":        item.get("why", ""),
            "blocked":    item.get("blocked", False),
            "stuck":      item.get("stuck", False),
            "in_queue_s": max(0, in_queue_s),
        })
    logger.debug("list_queue() → %d items", len(items))
    return items


def list_agents() -> list[dict]:
    """
    Return all Jenkins agents/nodes with online status and executor info.
    Each entry: {name, online, idle, executors, description, offline_reason}
    """
    url  = f"{_base_url()}/computer/api/json"
    tree = ("computer[displayName,description,offline,temporarilyOffline,"
            "numExecutors,idle,offlineCause[description]]")
    resp   = _get(url, params={"tree": tree})
    agents = []
    for c in resp.json().get("computer", []):
        cause  = c.get("offlineCause")
        reason = (cause.get("description", "") if isinstance(cause, dict) else "") or ""
        agents.append({
            "name":          c.get("displayName", ""),
            "description":   c.get("description", ""),
            "online":        not c.get("offline", False),
            "temp_offline":  c.get("temporarilyOffline", False),
            "executors":     c.get("numExecutors", 0),
            "idle":          c.get("idle", True),
            "offline_reason": reason,
        })
    logger.debug("list_agents() → %d agents", len(agents))
    return agents


# ═══════════════════════════════════════════════════════════════════════════════
#  GROUP B — Commits, comparisons, failed history, info, plugins
# ═══════════════════════════════════════════════════════════════════════════════

def list_build_changes(
    job_name: str,
    build_number: Optional[int] = None,
) -> dict:
    """
    Return git changesets (commits) included in a build.
    Uses lastBuild when build_number is None.
    Returns: {build_number, changes: [{author, message, date, commit_id, affected_files}]}
    """
    from urllib.parse import quote
    ref  = str(build_number) if build_number else "lastBuild"
    url  = f"{_base_url()}/job/{quote(job_name, safe='')}/{ref}/api/json"
    tree = "number,changeSet[items[author[fullName],msg,date,commitId,affectedPaths]]"
    resp = _get(url, params={"tree": tree})
    data = resp.json()

    actual_num = data.get("number", build_number)
    changes    = []
    for item in data.get("changeSet", {}).get("items", []):
        author = item.get("author", {})
        author_name = author.get("fullName", "") if isinstance(author, dict) else str(author)
        changes.append({
            "author":         author_name,
            "message":        (item.get("msg") or "").strip(),
            "date":           item.get("date", ""),
            "commit_id":      (item.get("commitId") or "")[:8],
            "affected_files": len(item.get("affectedPaths", [])),
        })

    logger.debug("list_build_changes(%r, #%s) → %d commits", job_name, ref, len(changes))
    return {"build_number": actual_num, "changes": changes}


def compare_builds(job_name: str, build_a: int, build_b: int) -> dict:
    """
    Compare two builds: result, duration, and change count.
    Returns a dict with side-by-side info for both builds.
    """
    from urllib.parse import quote

    def _fetch(n: int) -> dict:
        url  = f"{_base_url()}/job/{quote(job_name, safe='')}/{n}/api/json"
        tree = "number,result,duration,changeSet[items[author[fullName],msg]]"
        return _get(url, params={"tree": tree}).json()

    a = _fetch(build_a)
    b = _fetch(build_b)

    return {
        "job_name": job_name,
        "build_a": {
            "number":     a.get("number", build_a),
            "result":     a.get("result") or "UNKNOWN",
            "duration_s": a.get("duration", 0) // 1000,
            "changes":    len(a.get("changeSet", {}).get("items", [])),
        },
        "build_b": {
            "number":     b.get("number", build_b),
            "result":     b.get("result") or "UNKNOWN",
            "duration_s": b.get("duration", 0) // 1000,
            "changes":    len(b.get("changeSet", {}).get("items", [])),
        },
    }


def search_failed_builds(job_name: str, count: int = 20) -> list[dict]:
    """
    Return only the FAILURE/UNSTABLE/ABORTED builds from the last `count` builds.
    Reuses list_build_history (respects cache indirectly via Jenkins API).
    """
    history = list_build_history(job_name, count)
    failed  = [b for b in history if b["result"] in ("FAILURE", "UNSTABLE", "ABORTED")]
    logger.debug("search_failed_builds(%r) → %d/%d failed", job_name, len(failed), len(history))
    return failed


def get_jenkins_info() -> dict:
    """
    Return Jenkins server version, executor stats, and node health.
    Returns: {version, description, url, total_executors, busy_executors, offline_nodes}
    """
    try:
        data = _get(f"{_base_url()}/api/json", params={"tree": "version,nodeDescription,url"}).json()
        comp = _get(
            f"{_base_url()}/computer/api/json",
            params={"tree": "totalExecutors,busyExecutors,computer[offline]"},
        ).json()
        offline_nodes = sum(1 for c in comp.get("computer", []) if c.get("offline"))
        return {
            "version":         data.get("version", "unknown"),
            "description":     data.get("nodeDescription", ""),
            "url":             data.get("url", _base_url()),
            "total_executors": comp.get("totalExecutors", 0),
            "busy_executors":  comp.get("busyExecutors", 0),
            "offline_nodes":   offline_nodes,
        }
    except Exception as exc:
        logger.warning("get_jenkins_info failed: %s", exc)
        return {
            "version": "unknown", "description": "", "url": _base_url(),
            "total_executors": 0, "busy_executors": 0, "offline_nodes": 0,
        }


def list_plugins() -> list[dict]:
    """
    Return all installed Jenkins plugins sorted by display name.
    Each entry: {name, short_name, version, active, has_update}
    """
    url  = f"{_base_url()}/pluginManager/api/json"
    tree = "plugins[shortName,longName,version,active,enabled,hasUpdate]"
    resp = _get(url, params={"tree": tree, "depth": "1"})
    plugins = []
    for p in resp.json().get("plugins", []):
        plugins.append({
            "name":       p.get("longName") or p.get("shortName", ""),
            "short_name": p.get("shortName", ""),
            "version":    p.get("version", ""),
            "active":     p.get("active", False),
            "has_update": p.get("hasUpdate", False),
        })
    plugins.sort(key=lambda x: x["name"].lower())
    logger.debug("list_plugins() → %d plugins", len(plugins))
    return plugins


# ═══════════════════════════════════════════════════════════════════════════════
#  JOB BROWSER — filter, sort, paginate, repo matching
# ═══════════════════════════════════════════════════════════════════════════════

import math as _math

# Parameter names commonly used to hold a Git repository URL
_REPO_PARAM_NAMES = [
    "GITHUB_URL", "GIT_URL", "REPO_URL", "REPOSITORY_URL",
    "GIT_REPO", "REPOSITORY", "SOURCE_URL", "SCM_URL",
]


def get_job_repo_url(job_name: str) -> Optional[str]:
    """
    Try to find the Git repository URL configured for a Jenkins job.

    Checks in order:
    1. SCM configuration  (scm.userRemoteConfigs[0].url)
    2. Build parameter default values — any param whose name is in _REPO_PARAM_NAMES
       or the REPO_PARAM_NAME env var; uses _params_cache when available.

    Returns the URL string or None.
    """
    from urllib.parse import quote as _quote

    # 1. SCM config
    try:
        url  = f"{_base_url()}/job/{_quote(job_name, safe='')}/api/json"
        data = _get(url, params={"tree": "scm[userRemoteConfigs[url]]"}).json()
        scm  = data.get("scm") or {}
        configs = scm.get("userRemoteConfigs", [])
        if configs and configs[0].get("url"):
            return configs[0]["url"]
    except Exception:
        pass

    # 2. Parameter defaults (use cache when available)
    keys = list(_REPO_PARAM_NAMES)
    custom = os.getenv("REPO_PARAM_NAME", "").strip().upper()
    if custom and custom not in keys:
        keys.insert(0, custom)

    try:
        cached = _params_cache.get(job_name)
        params = cached[0] if cached else get_job_parameters(job_name)
        for p in params:
            if p.get("name", "").upper() in keys:
                val = str(p.get("default") or "")
                if val and ("/" in val or "github" in val.lower() or "git" in val.lower()):
                    return val
    except Exception:
        pass

    return None


def filter_and_page_jobs(
    jobs:            list[dict],
    q:               str  = "",
    status:          str  = "",
    hotfix:          bool = False,
    repo:            str  = "",
    branch:          str  = "",
    repo_param_key:  str  = "",
    page:            int  = 1,
    per_page:        int  = 20,
    sort:            str  = "name",
) -> dict:
    """
    Filter, sort, and paginate a list of Jenkins jobs in memory.

    Parameters
    ----------
    jobs           : Full list from list_jobs() (already cached).
    q              : Free-text search — job name, description, URL.
    status         : Build status: ALL|SUCCESS|FAILURE|BUILDING|UNSTABLE|ABORTED.
    hotfix         : True → only jobs with hotfix/HF/HOTFIX in the name.
    repo           : Repository substring — checked in name/description/URL,
                     then in GITHUB_URL-like param defaults (cache only).
    branch         : Branch substring — checked in name/description.
    repo_param_key : Override the parameter name used for repo matching.
    page           : 1-based page number.
    per_page       : Items per page, clamped 5–50.
    sort           : "name" | "status" | "failed" | "duration"

    Returns
    -------
    {jobs, total, page, per_page, total_pages, active_filters, repo_matched_by}
    """
    per_page = max(5, min(50, per_page))
    page     = max(1, page)

    filtered        = list(jobs)
    active_filters: dict = {}
    repo_matched_by: Optional[str] = None

    # ── Free-text search ─────────────────────────────────────────────────────
    if q:
        terms = q.lower().split()
        filtered = [
            j for j in filtered
            if all(
                t in j.get("name", "").lower() or
                t in j.get("description", "").lower() or
                t in j.get("url", "").lower()
                for t in terms
            )
        ]
        active_filters["q"] = q

    # ── Status filter ─────────────────────────────────────────────────────────
    if status and status.upper() not in ("ALL", ""):
        s = status.upper()
        _STATUS_MAP: dict = {
            "FAILURE":  ["last build: failed"],
            "SUCCESS":  ["last build: success"],
            "BUILDING": ["building"],
            "UNSTABLE": ["last build: unstable"],
            "ABORTED":  ["last build: aborted"],
        }
        targets = _STATUS_MAP.get(s, [s.lower()])
        filtered = [
            j for j in filtered
            if any(t in j.get("status", "").lower() for t in targets)
        ]
        active_filters["status"] = s

    # ── Hotfix filter ─────────────────────────────────────────────────────────
    if hotfix:
        # Match: hotfix, HOTFIX, HF (whole word), hot-fix, hot_fix
        _HF_RE = re.compile(r'\b(hotfix|hf)\b|hot[\-_]fix', re.IGNORECASE)
        filtered = [j for j in filtered if _HF_RE.search(j.get("name", ""))]
        active_filters["hotfix"] = True

    # ── Branch filter ─────────────────────────────────────────────────────────
    if branch:
        bl = branch.lower()
        filtered = [
            j for j in filtered
            if bl in j.get("name", "").lower() or
               bl in j.get("description", "").lower()
        ]
        active_filters["branch"] = branch

    # ── Repo filter ───────────────────────────────────────────────────────────
    if repo:
        rl = repo.lower()

        # Pass 1: cheap — name / description / URL substring
        name_match = [
            j for j in filtered
            if rl in j.get("name", "").lower() or
               rl in j.get("description", "").lower() or
               rl in j.get("url", "").lower()
        ]
        if name_match:
            filtered        = name_match
            repo_matched_by = "job name / description"
        else:
            # Pass 2: check GITHUB_URL-like param defaults (cache only, no extra calls)
            keys = list(_REPO_PARAM_NAMES)
            if repo_param_key:
                keys.insert(0, repo_param_key.upper())
            custom = os.getenv("REPO_PARAM_NAME", "").strip().upper()
            if custom and custom not in keys:
                keys.insert(0, custom)

            param_match = []
            for j in filtered:
                cached = _params_cache.get(j["name"])
                if not cached:
                    continue
                for p in cached[0]:
                    if p.get("name", "").upper() in keys:
                        if rl in str(p.get("default") or "").lower():
                            param_match.append(j)
                            break

            if param_match:
                filtered        = param_match
                repo_matched_by = f"parameter `{keys[0]}`"

        active_filters["repo"] = repo

    # ── Sort ──────────────────────────────────────────────────────────────────
    def _key(j: dict):
        lb = j.get("last_build") or {}
        if sort == "status":
            order = {
                "last build: failed": 0, "building": 1,
                "last build: unstable": 2, "last build: success": 3,
            }
            return order.get(j.get("status", "").lower(), 4)
        elif sort == "failed":
            result = lb.get("result") or ""
            ts     = lb.get("timestamp_s", 0) if result in ("FAILURE", "UNSTABLE") else 0
            return -ts
        elif sort == "duration":
            return -(lb.get("duration_ms") or 0)
        else:   # "name"
            return j.get("name", "").lower()

    filtered.sort(key=_key)

    # ── Paginate ──────────────────────────────────────────────────────────────
    total     = len(filtered)
    start     = (page - 1) * per_page
    end       = start + per_page
    page_jobs = filtered[start:end]

    return {
        "jobs":            page_jobs,
        "total":           total,
        "page":            page,
        "per_page":        per_page,
        "total_pages":     max(1, _math.ceil(total / per_page)),
        "active_filters":  active_filters,
        "repo_matched_by": repo_matched_by,
    }

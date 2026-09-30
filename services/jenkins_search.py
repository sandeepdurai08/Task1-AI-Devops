"""
services/jenkins_search.py
──────────────────────────
Background job-configuration indexer and advanced search engine for BuildBot.
"""

import logging
import math
import os
import re
import threading
import time
try:
    import defusedxml.ElementTree as ET  # safer parser — prevents XXE attacks
except ImportError:
    import xml.etree.ElementTree as ET   # fallback: stdlib (internal server only)  # nosec B405

from typing import Optional
from urllib.parse import quote as _url_quote

logger = logging.getLogger(__name__)

SEARCH_INDEX_TTL_S = int(os.getenv("SEARCH_INDEX_TTL", "600"))
_MAX_FOLDER_DEPTH  = 8
_HTTP_TIMEOUT_S    = 15

_REPO_PARAM_KEYS = {
    "GITHUB_URL", "GIT_URL", "REPO_URL", "REPOSITORY_URL",
    "GIT_REPO", "REPOSITORY", "SOURCE_URL", "SCM_URL",
}

_HOTFIX_RE = re.compile(r'\b(hotfix|hf)\b|hot[\-_]fix|\bpatch\b', re.IGNORECASE)

_COLOR_TO_SHORT = {
    "blue": "success",   "blue_anime": "building",
    "red":  "failed",    "red_anime":  "building",
    "yellow": "unstable","yellow_anime": "building",
    "grey": "never_built", "disabled": "disabled",
    "aborted": "aborted",  "notbuilt": "never_built",
}
_SHORT_TO_LONG = {
    "success":    "last build: SUCCESS",
    "failed":     "last build: FAILED",
    "unstable":   "last build: UNSTABLE",
    "aborted":    "last build: ABORTED",
    "building":   "building",
    "never_built": "never built",
    "disabled":   "disabled",
}
_STATUS_KW: dict[str, list[str]] = {
    "SUCCESS":  ["success"],
    "FAILURE":  ["fail"],
    "BUILDING": ["building"],
    "UNSTABLE": ["unstable"],
    "ABORTED":  ["aborted"],
}


# ═══════════════════════════════════════════════════════════════════════════════
#  XML HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _xml_text(el, *tags, default: str = "") -> str:
    cur = el
    for tag in tags:
        cur = cur.find(tag)
        if cur is None:
            return default
    return (cur.text or "").strip()


def _parse_scm_urls(root) -> list[str]:
    urls: list[str] = []
    for scm in root.iter("scm"):
        for cfg in scm.iter("userRemoteConfigs"):
            for rc in cfg:
                u = _xml_text(rc, "url")
                if u:
                    urls.append(u)
        for repo in scm.iter("remoteRepositories"):
            for rc in repo:
                u = _xml_text(rc, "url")
                if u:
                    urls.append(u)
    for src in root.iter("sources"):
        for item in src:
            u = _xml_text(item, "remote") or _xml_text(item, "url")
            if u:
                urls.append(u)
    for remote_el in root.iter("remote"):
        u = (remote_el.text or "").strip()
        if u and "://" in u:
            urls.append(u)
    return list(dict.fromkeys(filter(None, urls)))


def _parse_scm_branches(root) -> list[str]:
    branches: list[str] = []
    for scm in root.iter("scm"):
        for branches_el in scm.iter("branches"):
            for spec in branches_el:
                n = _xml_text(spec, "name")
                if n:
                    branches.append(n)
    return list(dict.fromkeys(filter(None, branches)))


def _parse_scm(root) -> tuple[list, list]:
    return _parse_scm_urls(root), _parse_scm_branches(root)


def _parse_parameters(root) -> tuple[list, dict]:
    parameters: list[dict] = []
    param_map: dict[str, str] = {}
    for props in root.iter("parameterDefinitions"):
        for pd in props:
            name = _xml_text(pd, "name")
            if not name:
                continue
            default = (
                _xml_text(pd, "defaultParameterValue", "value")
                or _xml_text(pd, "default")
                or ""
            )
            choices: list[str] = []
            choices_el = pd.find("choices")
            if choices_el is not None:
                choices = [ch.text.strip() for ch in choices_el.iter("string") if ch.text]
            parameters.append({"name": name, "default": default,
                                "type": pd.tag.lower(), "choices": choices})
            param_map[name] = default
    return parameters, param_map


def _parse_pipeline(root, scm_urls: list) -> tuple[str, str]:
    pipeline_script = ""
    script_path = ""
    for defn in root.iter("definition"):
        cls = defn.get("class", "").lower()
        if "cpsflowdefinition" in cls or "flow-definition" in cls:
            script_el = defn.find("script")
            if script_el is not None and script_el.text:
                pipeline_script = script_el.text.strip()
        elif "cpsscmflowdefinition" in cls or "cpsscm" in cls:
            sp = defn.find("scriptPath")
            if sp is not None and sp.text:
                script_path = sp.text.strip()
            for rc in defn.iter("userRemoteConfigs"):
                for item in rc:
                    u = _xml_text(item, "url")
                    if u and u not in scm_urls:
                        scm_urls.append(u)
    if not pipeline_script:
        for s in root.iter("script"):
            if s.text and len(s.text.strip()) > 20:
                pipeline_script = s.text.strip()
                break
    return pipeline_script, script_path


def _parse_env_vars(root) -> dict:
    env_vars: dict[str, str] = {}
    for env_el in root.iter("propertiesContent"):
        if env_el.text:
            for line in env_el.text.splitlines():
                if "=" in line:
                    k, _, v = line.partition("=")
                    k = k.strip()
                    if k:
                        env_vars[k] = v.strip()
    for gp in root.iter("globalNodeProperties"):
        for entry in gp.iter("entry"):
            strings = [s.text.strip() for s in entry.iter("string") if s.text]
            if len(strings) >= 2:
                env_vars[strings[0]] = strings[1]
    return env_vars


def _infer_job_type(tag: str) -> str:
    if "folder" in tag or "organizationfolder" in tag:
        return "folder"
    if "workflowmultibranch" in tag or "multibranch" in tag:
        return "multibranch"
    if "workflowjob" in tag or "flow-definition" in tag:
        return "pipeline"
    return "freestyle"


def parse_config_xml(xml_text: str, full_name: str = "") -> dict:
    """Parse a Jenkins config.xml and return searchable metadata."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        logger.debug("parse_config_xml: XML parse error for %r: %s", full_name, exc)
        return _empty_config()

    job_type                     = _infer_job_type(root.tag.lower())
    description                  = _xml_text(root, "description")
    scm_urls, scm_branches       = _parse_scm(root)
    parameters, param_map        = _parse_parameters(root)
    pipeline_script, script_path = _parse_pipeline(root, scm_urls)
    env_vars                     = _parse_env_vars(root)

    shared_libraries: list[str] = [
        _xml_text(lib, "name") for lib in root.iter("library") if _xml_text(lib, "name")
    ]
    if pipeline_script:
        for m in re.finditer(r'@Library\s*\(\s*["\']([^"\']+)["\']', pipeline_script):
            lib_name = m.group(1).split("@")[0].strip()
            if lib_name and lib_name not in shared_libraries:
                shared_libraries.append(lib_name)

    return {
        "job_type":         job_type,
        "scm_urls":         scm_urls,
        "scm_branches":     scm_branches,
        "parameters":       parameters,
        "param_map":        param_map,
        "pipeline_script":  pipeline_script,
        "script_path":      script_path,
        "shared_libraries": shared_libraries,
        "description":      description,
        "env_vars":         env_vars,
        "config_restricted": False,
    }


def _empty_config(restricted: bool = False) -> dict:
    return {
        "job_type":         "unknown",
        "scm_urls":         [],
        "scm_branches":     [],
        "parameters":       [],
        "param_map":        {},
        "pipeline_script":  "",
        "script_path":      "",
        "shared_libraries": [],
        "description":      "",
        "env_vars":         {},
        "config_restricted": restricted,
    }


# ═══════════════════════════════════════════════════════════════════════════════
#  JENKINS REST HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _jenkins_base() -> str:
    return os.getenv("JENKINS_URL", "http://localhost:8080").rstrip("/")


def _make_api_url(base_url: str, folder_path: str) -> str:
    if not folder_path:
        return f"{base_url}/api/json"
    parts    = folder_path.split("/")
    url_path = "/".join(f"job/{_url_quote(p, safe='')}" for p in parts)
    return f"{base_url}/{url_path}/api/json"


def _fetch_jobs_page(api_url: str, auth) -> list[dict]:
    """Fetch one page of jobs from the Jenkins API. Returns [] on any error."""
    import requests
    from requests.exceptions import ConnectionError as ReqConnErr, Timeout

    tree = (
        "jobs[name,url,color,description,"
        "lastBuild[number,result,duration,timestamp],"
        "jobs[name]]"
    )
    try:
        resp = requests.get(api_url, auth=auth, timeout=_HTTP_TIMEOUT_S,
                            params={"tree": tree})
        if not resp.ok:
            logger.debug("_fetch_jobs_page %r → HTTP %s", api_url, resp.status_code)
            return []
        return resp.json().get("jobs", [])
    except (ReqConnErr, Timeout) as exc:
        logger.warning("_fetch_jobs_page: connection error at %r: %s", api_url, exc)
        return []
    except Exception as exc:
        logger.debug("_fetch_jobs_page %r error: %s", api_url, exc)
        return []


def _job_to_dict(job: dict, folder_path: str) -> dict:
    """Convert a raw Jenkins API job entry to a normalised dict."""
    name   = job.get("name", "")
    color  = job.get("color", "grey")
    lb     = job.get("lastBuild") or {}
    short  = _COLOR_TO_SHORT.get(color.replace("_anime", ""), "unknown")
    return {
        "name":        name,
        "full_name":   f"{folder_path}/{name}" if folder_path else name,
        "folder":      folder_path,
        "url":         job.get("url", ""),
        "description": (job.get("description") or "").strip(),
        "color":       color,
        "status":      _SHORT_TO_LONG.get(short, short),
        "status_short": short,
        "building":    color.endswith("_anime"),
        "last_build":  {
            "number":      lb.get("number"),
            "result":      lb.get("result"),
            "duration_ms": lb.get("duration", 0),
            "timestamp_s": (lb.get("timestamp") or 0) // 1000,
        } if lb else None,
    }


def _discover_jobs(base_url: str, auth, folder_path: str = "",
                   depth: int = 0) -> list[dict]:
    """Recursively discover all leaf Jenkins jobs under a folder path."""
    if depth > _MAX_FOLDER_DEPTH:
        return []

    api_url  = _make_api_url(base_url, folder_path)
    raw_jobs = _fetch_jobs_page(api_url, auth)

    results: list[dict] = []
    for job in raw_jobs:
        name      = job.get("name", "")
        full_name = f"{folder_path}/{name}" if folder_path else name
        if job.get("jobs") is not None:
            results.extend(_discover_jobs(base_url, auth, full_name, depth + 1))
        else:
            results.append(_job_to_dict(job, folder_path))
    return results


def _fetch_config_xml(full_name: str, auth) -> Optional[str]:
    """GET config.xml for a job. Returns None on 403/404/network error."""
    import requests
    from requests.exceptions import ConnectionError as ReqConnErr, Timeout

    parts    = full_name.split("/")
    url_path = "/".join(f"job/{_url_quote(p, safe='')}" for p in parts)
    url      = f"{_jenkins_base()}/{url_path}/config.xml"

    try:
        resp = requests.get(url, auth=auth, timeout=_HTTP_TIMEOUT_S)
        if resp.status_code in (403, 404):
            logger.debug("config.xml %r → HTTP %s (skip)", full_name, resp.status_code)
            return None
        resp.raise_for_status()
        return resp.text
    except (ReqConnErr, Timeout):
        return None
    except Exception as exc:
        logger.debug("_fetch_config_xml(%r): %s", full_name, exc)
        return None


# ═══════════════════════════════════════════════════════════════════════════════
#  ENTRY BUILDER + SEARCHABLE TEXT
# ═══════════════════════════════════════════════════════════════════════════════

def _first_set(pm: dict, *keys: str) -> str:
    return next((pm[k] for k in keys if pm.get(k)), "")


def _build_entry(job: dict, cfg: dict) -> dict:
    """Merge a raw job dict with parsed config.xml data into a full index entry."""
    pm       = cfg["param_map"]
    scm_urls = list(cfg["scm_urls"])
    for p in cfg["parameters"]:
        if p["name"].upper() in _REPO_PARAM_KEYS:
            val = str(p.get("default") or "").strip()
            if val and val not in scm_urls:
                scm_urls.append(val)

    env_type     = _first_set(pm, "ENV_TYPE", "ENVIRONMENT", "ENV", "DEPLOY_ENV", "TARGET_ENV").lower()
    aws_region   = _first_set(pm, "AWS_REGION", "REGION", "AWS_DEFAULT_REGION").lower()
    cluster_name = _first_set(pm, "CLUSTER_NAME", "CLUSTER", "K8S_CLUSTER",
                               "EKS_CLUSTER", "KUBE_CLUSTER", "AKS_CLUSTER").lower()

    job_type = cfg["job_type"]
    if job_type == "unknown":
        job_type = "pipeline" if (cfg["pipeline_script"] or cfg["script_path"]) else "freestyle"

    entry: dict = {
        **job,
        "description":      cfg["description"] or job.get("description", ""),
        "job_type":         job_type,
        "scm_urls":         scm_urls,
        "scm_branches":     cfg["scm_branches"],
        "parameters":       cfg["parameters"],
        "param_map":        pm,
        "pipeline_script":  cfg["pipeline_script"],
        "script_path":      cfg["script_path"],
        "shared_libraries": cfg["shared_libraries"],
        "env_vars":         cfg["env_vars"],
        "env_type":         env_type,
        "aws_region":       aws_region,
        "cluster_name":     cluster_name,
        "is_hotfix":        bool(_HOTFIX_RE.search(job["name"])),
        "config_restricted": cfg["config_restricted"],
    }
    entry["_searchable_text"] = _build_searchable_text(entry)
    return entry


def _build_searchable_text(e: dict) -> str:
    parts = [e.get("full_name", ""), e.get("name", ""),
             e.get("description", ""), e.get("folder", "")]
    parts += e.get("scm_urls", [])
    parts += e.get("scm_branches", [])
    for p in e.get("parameters", []):
        parts.append(p.get("name", ""))
        parts.append(str(p.get("default", "")))
        parts += p.get("choices", [])
    parts += e.get("shared_libraries", [])
    if e.get("pipeline_script"):
        parts.append(e["pipeline_script"])
    if e.get("script_path"):
        parts.append(e["script_path"])
    for k, v in e.get("env_vars", {}).items():
        parts += [k, str(v)]
    for field in ("env_type", "aws_region", "cluster_name"):
        if e.get(field):
            parts.append(e[field])
    return " ".join(filter(None, parts)).lower()


def _strip_internal(e: dict) -> dict:
    return {k: v for k, v in e.items() if k != "_searchable_text"}


# ═══════════════════════════════════════════════════════════════════════════════
#  SEARCH FILTERS + SORT
# ═══════════════════════════════════════════════════════════════════════════════

def _filter_text(entries: list, q: str, af: dict) -> list:
    if not q:
        return entries
    terms = q.lower().split()
    af["q"] = q
    return [e for e in entries if all(t in e["_searchable_text"] for t in terms)]


def _filter_repo(entries: list, repo: str, af: dict) -> list:
    if not repo:
        return entries
    af["repo"] = repo
    rl = repo.lower()
    return [e for e in entries if rl in e["_searchable_text"]]


def _filter_branch(entries: list, branch: str, af: dict) -> list:
    if not branch:
        return entries
    af["branch"] = branch
    bl = branch.lower()
    return [e for e in entries
            if bl in e.get("name", "").lower() or bl in e.get("_searchable_text", "")]


def _filter_status(entries: list, status: str, af: dict) -> list:
    if not status or status.upper() in ("ALL", ""):
        return entries
    s   = status.upper()
    kws = _STATUS_KW.get(s, [s.lower()])
    af["status"] = s
    return [e for e in entries
            if any(k in e.get("status", "").lower() for k in kws)
            or (e.get("building") and s == "BUILDING")]


def _filter_simple(entries: list, af: dict, **kwargs) -> list:
    """Apply simple substring/equality filters: hotfix, folder, job_type,
    param_name, param_value, env_type, aws_region, cluster_name."""
    if kwargs.get("hotfix"):
        entries = [e for e in entries if e.get("is_hotfix")]
        af["hotfix"] = True
    if kwargs.get("folder"):
        fl = kwargs["folder"].lower()
        entries = [e for e in entries
                   if fl in e.get("folder", "").lower() or fl in e.get("full_name", "").lower()]
        af["folder"] = kwargs["folder"]
    if kwargs.get("job_type"):
        jt = kwargs["job_type"].lower()
        entries = [e for e in entries if e.get("job_type", "").lower() == jt]
        af["job_type"] = kwargs["job_type"]
    if kwargs.get("param_name"):
        pnl = kwargs["param_name"].lower()
        entries = [e for e in entries
                   if any(p.get("name", "").lower() == pnl for p in e.get("parameters", []))]
        af["param_name"] = kwargs["param_name"]
    if kwargs.get("param_value"):
        pvl = kwargs["param_value"].lower()
        entries = [e for e in entries
                   if any(pvl in str(p.get("default", "")).lower()
                          for p in e.get("parameters", []))]
        af["param_value"] = kwargs["param_value"]
    for field in ("env_type", "aws_region", "cluster_name"):
        val = kwargs.get(field, "")
        if val:
            entries = [e for e in entries if val.lower() in e.get(field, "")]
            af[field] = val
    return entries


def _apply_filters(
    entries: list[dict],
    q: str, repo: str, branch: str, status: str,
    hotfix: bool, folder: str, job_type: str,
    param_name: str, param_value: str,
    env_type: str, aws_region: str, cluster_name: str,
) -> tuple[list[dict], dict]:
    af: dict = {}
    entries = _filter_text(entries, q, af)
    entries = _filter_repo(entries, repo, af)
    entries = _filter_branch(entries, branch, af)
    entries = _filter_status(entries, status, af)
    entries = _filter_simple(
        entries, af,
        hotfix=hotfix, folder=folder, job_type=job_type,
        param_name=param_name, param_value=param_value,
        env_type=env_type, aws_region=aws_region, cluster_name=cluster_name,
    )
    return entries, af


def _make_sort_key(sort: str):
    def key(e: dict):
        lb = e.get("last_build") or {}
        if sort == "status":
            order = {"failed": 0, "building": 1, "unstable": 2, "success": 3, "never_built": 4}
            return order.get(e.get("status_short", ""), 5)
        if sort == "failed":
            result = lb.get("result") or ""
            return -(lb.get("timestamp_s", 0) if result in ("FAILURE", "UNSTABLE") else 0)
        if sort == "duration":
            return -(lb.get("duration_ms") or 0)
        if sort == "folder":
            return (e.get("folder", "").lower(), e.get("name", "").lower())
        return e.get("full_name", e.get("name", "")).lower()
    return key


# ═══════════════════════════════════════════════════════════════════════════════
#  JOB SEARCH INDEX
# ═══════════════════════════════════════════════════════════════════════════════

class JobSearchIndex:
    """Singleton background indexer."""

    def __init__(self) -> None:
        self._lock      = threading.Lock()
        self._entries:  list[dict] = []
        self._ready     = threading.Event()
        self._built_at  = 0.0
        self._job_count = 0
        self._building  = False
        self._errors:   list[str] = []

    def start(self) -> None:
        t = threading.Thread(target=self._run_loop, name="search-indexer", daemon=True)
        t.start()
        logger.info("JobSearchIndex: started (TTL=%ds, max_depth=%d)",
                    SEARCH_INDEX_TTL_S, _MAX_FOLDER_DEPTH)

    def wait_ready(self, timeout: float = 30.0) -> bool:
        return self._ready.wait(timeout)

    def _run_loop(self) -> None:
        while True:
            self._build_index()
            time.sleep(SEARCH_INDEX_TTL_S)

    def _build_index(self) -> None:
        from services.jenkins_client import _auth, JenkinsError

        with self._lock:
            self._building = True
            self._errors   = []

        logger.info("JobSearchIndex: index build starting…")
        t0 = time.monotonic()

        try:
            auth = _auth()
        except JenkinsError as exc:
            logger.warning("JobSearchIndex: auth error — %s", exc)
            with self._lock:
                self._building = False
                self._errors   = [str(exc)]
            self._ready.set()
            return

        raw_jobs = _discover_jobs(_jenkins_base(), auth)
        logger.info("JobSearchIndex: discovered %d jobs in %.1fs",
                    len(raw_jobs), time.monotonic() - t0)

        entries = [
            _build_entry(job, parse_config_xml(xml, job["full_name"]) if (xml := _fetch_config_xml(job["full_name"], auth)) else _empty_config())
            for job in raw_jobs
        ]

        logger.info("JobSearchIndex: %d entries built in %.1fs",
                    len(entries), time.monotonic() - t0)

        with self._lock:
            self._entries   = entries
            self._built_at  = time.time()
            self._job_count = len(entries)
            self._building  = False

        self._ready.set()

    def status(self) -> dict:
        with self._lock:
            return {
                "job_count": self._job_count,
                "built_at":  self._built_at,
                "building":  self._building,
                "age_s":     int(time.time() - self._built_at) if self._built_at else None,
                "errors":    list(self._errors),
                "ttl_s":     SEARCH_INDEX_TTL_S,
            }

    def snapshot(self) -> list[dict]:
        with self._lock:
            return [_strip_internal(e) for e in self._entries]

    def snapshot_raw(self) -> list[dict]:
        with self._lock:
            return list(self._entries)

    def search(
        self,
        q:            str  = "",
        repo:         str  = "",
        branch:       str  = "",
        status:       str  = "",
        hotfix:       bool = False,
        folder:       str  = "",
        job_type:     str  = "",
        param_name:   str  = "",
        param_value:  str  = "",
        env_type:     str  = "",
        aws_region:   str  = "",
        cluster_name: str  = "",
        page:         int  = 1,
        per_page:     int  = 6,
        sort:         str  = "name",
    ) -> dict:
        per_page = max(1, min(50, per_page))
        page     = max(1, page)

        with self._lock:
            entries = list(self._entries)

        filtered, active_filters = _apply_filters(
            entries, q, repo, branch, status, hotfix, folder,
            job_type, param_name, param_value, env_type, aws_region, cluster_name,
        )
        filtered.sort(key=_make_sort_key(sort))

        total      = len(filtered)
        start      = (page - 1) * per_page
        page_items = filtered[start: start + per_page]

        return {
            "jobs":           [_strip_internal(e) for e in page_items],
            "total":          total,
            "page":           page,
            "per_page":       per_page,
            "total_pages":    max(1, math.ceil(total / per_page)),
            "active_filters": active_filters,
            "index_status":   self.status(),
        }


# ═══════════════════════════════════════════════════════════════════════════════
#  SINGLETON
# ═══════════════════════════════════════════════════════════════════════════════

_index: Optional[JobSearchIndex] = None
_index_lock = threading.Lock()


def get_index() -> JobSearchIndex:
    global _index
    if _index is None:
        with _index_lock:
            if _index is None:
                _index = JobSearchIndex()
    return _index

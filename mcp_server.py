#!/usr/bin/env python3
# True MCP JSON-RPC server over stdio + adapters: GitHub/Jira/Actions Logs
from __future__ import annotations
import json, sys, time, traceback, hashlib, os
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

# ── adapters you pasted ───────────────────────────────────────────────────────
from adapters.actions_metrics import ActionsMetrics
from adapters.log_store import LogStore
from adapters.github_adapter import GitHub
from adapters.jira_adapter import Jira

JSONRPC_VERSION = "2.0"
AUDIT_PATH = os.getenv("AUDIT_LOG", "audit.log")

# ── tiny tamper-evident audit log (hash chain) ───────────────────────────────
def _last_hash(path: str) -> str:
    try:
        with open(path, "rb") as f:
            last = b""
            for line in f:
                if line.strip():
                    last = line.strip()
            if not last:
                return "0"*64
            obj = json.loads(last.decode("utf-8", errors="ignore"))
            return obj.get("hash", "0"*64)
    except FileNotFoundError:
        return "0"*64

def audit_write(event: str, payload: Dict[str, Any], result: Dict[str, Any], ok: bool, t_ms: float):
    prev = _last_hash(AUDIT_PATH)
    entry = {
        "ts": time.time(),
        "event": event,
        "ok": ok,
        "t_ms": round(t_ms, 3),
        "payload": payload,
        "result": result,
        "prev": prev,
    }
    h = hashlib.sha256(json.dumps(entry, sort_keys=True).encode("utf-8")).hexdigest()
    entry["hash"] = h
    with open(AUDIT_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")

# ── schemas (core tools) ─────────────────────────────────────────────────────
class FlakyRequest(BaseModel):
    test_name: str = Field(..., description="Test identifier")
    history: List[str] = Field(..., description='Sequence of "pass"/"fail" outcomes')

class FlakyResponse(BaseModel):
    flaky: bool
    failures: int
    runs: int
    label: str

class SuggestFixRequest(BaseModel):
    test_name: str
    history: List[str]

class SuggestFixResponse(BaseModel):
    suggestions: List[str]

# ── schemas (adapters + aggregate) ───────────────────────────────────────────
class ActionsMetricsRequest(BaseModel):
    repo: str
    branch: Optional[str] = None

class ActionsMetricsResponse(BaseModel):
    total: int
    passed: int
    failed: int
    pass_rate: float

class GetLogSnippetsRequest(BaseModel):
    repo: str
    run_id: int
    max_files: int = 10
    max_snippets: int = 20

class GetLogSnippetsResponse(BaseModel):
    files_preview: List[str]
    snippets: List[str]

class OpenPRRequest(BaseModel):
    repo: str                 # owner/repo
    head: str                 # branch or SHA
    base: str                 # target branch (e.g., main)
    title: str
    body: str = ""
    draft: bool = True

class OpenPRResponse(BaseModel):
    number: int
    url: str
    html_url: str
    state: str
    title: str
    head: str
    base: str

class CreateJiraRequest(BaseModel):
    project_key: str
    summary: str
    description: str = ""
    issue_type: str = "Task"

class CreateJiraResponse(BaseModel):
    key: str
    id: str

class ClassifyAggregateRequest(BaseModel):
    test_name: str
    repo: Optional[str] = None       # owner/repo for metrics/logs
    run_id: Optional[int] = None     # actions run id for logs
    history: Optional[List[str]] = None
    max_log_snippets: int = 20

class Evidence(BaseModel):
    pass_rate: float
    runs_total: int
    log_snippets: List[str]

class ClassifyAggregateResponse(BaseModel):
    label: str
    flaky: bool
    score: Dict[str, float]
    failures: int
    runs: int
    evidence: Evidence
    reasons: List[str]

# ── logic (core tools) ───────────────────────────────────────────────────────
def _classify_simple(history: List[str]) -> Dict[str, Any]:
    h = [s.strip().lower() for s in history if s.strip()]
    n = len(h)
    fails = sum(s == "fail" for s in h)
    mixed = ("fail" in h and "pass" in h)
    rate = fails / max(n, 1)
    label = "Stable"
    if mixed and 0.1 <= rate <= 0.9:
        label = "Flaky"
    elif rate > 0.0 and not mixed:
        label = "Regressing"
    return {"label": label, "failures": fails, "runs": n, "flaky": label == "Flaky"}

def tool_is_flaky(req: FlakyRequest) -> FlakyResponse:
    res = _classify_simple(req.history)
    return FlakyResponse(flaky=res["flaky"], failures=res["failures"], runs=res["runs"], label=res["label"])

def tool_suggest_fix(req: SuggestFixRequest) -> SuggestFixResponse:
    tips = set()
    h = [s.strip().lower() for s in req.history if s.strip()]
    if "fail" in h and "pass" in h:
        tips.add("Seed RNG; replace time.sleep with condition-based waits.")
        tips.add("Freeze time (freezegun/fake timers) to eliminate clock drift.")
    tips.add("Mock external deps (network/files/db) to remove nondeterminism.")
    tips.add("Ensure test order independence; isolate global state and I/O.")
    return SuggestFixResponse(suggestions=sorted(tips))

# ── logic (adapters) ─────────────────────────────────────────────────────────
def tool_get_actions_metrics(req: ActionsMetricsRequest) -> ActionsMetricsResponse:
    t0 = time.perf_counter()
    am = ActionsMetrics()
    runs = am.list_runs(req.repo, branch=req.branch)
    m = am.summarize(runs)
    t_ms = (time.perf_counter() - t0) * 1000.0
    audit_write("get_actions_metrics", req.model_dump(), m.model_dump(), ok=True, t_ms=t_ms)
    return ActionsMetricsResponse(**m.model_dump())

def tool_get_ci_log_snippets(req: GetLogSnippetsRequest) -> GetLogSnippetsResponse:
    t0 = time.perf_counter()
    ls = LogStore()
    z = ls.fetch_run_logs_zip(req.repo, req.run_id)
    names = ls.list_log_files(z, limit=req.max_files)
    snips = ls.extract_failure_snippets(z, max_files=req.max_files, max_snippets=req.max_snippets)
    t_ms = (time.perf_counter() - t0) * 1000.0
    audit_write("get_ci_log_snippets", req.model_dump(),
                {"files_preview": names[:5], "snippets_len": len(snips)}, ok=True, t_ms=t_ms)
    return GetLogSnippetsResponse(files_preview=names[:20], snippets=snips)

def tool_open_pr(req: OpenPRRequest) -> OpenPRResponse:
    t0 = time.perf_counter()
    gh = GitHub()
    pr = gh.open_pr(req.repo, req.head, req.base, req.title, req.body, req.draft)
    t_ms = (time.perf_counter() - t0) * 1000.0
    audit_write("open_pr", req.model_dump(), pr.model_dump(), ok=True, t_ms=t_ms)
    return OpenPRResponse(**pr.model_dump())

def tool_create_jira(req: CreateJiraRequest) -> CreateJiraResponse:
    t0 = time.perf_counter()
    j = Jira()
    out = j.create_issue(req.project_key, req.summary, req.description, req.issue_type)
    t_ms = (time.perf_counter() - t0) * 1000.0
    audit_write("create_jira", req.model_dump(), out, ok=True, t_ms=t_ms)
    return CreateJiraResponse(key=out["key"], id=str(out["id"]))

# ── logic (aggregate: history + metrics + logs) ──────────────────────────────
INFRA_PATTERNS = ("connection reset", "timeout", "503", "network is unreachable", "dns", "rate limit")

def tool_classify_aggregate(req: ClassifyAggregateRequest) -> ClassifyAggregateResponse:
    t0 = time.perf_counter()
    reasons: List[str] = []
    pass_rate = 0.0
    runs_total = 0
    log_snips: List[str] = []

    # metrics
    if req.repo:
        am = ActionsMetrics()
        runs = am.list_runs(req.repo)
        m = am.summarize(runs)
        pass_rate = m.pass_rate
        runs_total = m.total
        reasons.append(f"Actions pass_rate={pass_rate}% over {runs_total} runs.")

    # logs
    if req.repo and req.run_id:
        ls = LogStore()
        z = ls.fetch_run_logs_zip(req.repo, req.run_id)
        log_snips = ls.extract_failure_snippets(z, max_files=10, max_snippets=req.max_log_snippets)
        if log_snips:
            reasons.append(f"Collected {len(log_snips)} error lines from CI logs.")

    # history
    failures = 0
    runs = 0
    base_label = "Unknown"
    base_flaky = False
    if req.history:
        base = _classify_simple(req.history)
        base_label = base["label"]
        base_flaky = base["flaky"]
        failures = base["failures"]
        runs = base["runs"]

    score = {"flake": 0.0, "regression": 0.0, "infra": 0.0}
    # history signal
    if base_label == "Flaky":
        score["flake"] += 0.6; reasons.append("Mixed pass/fail history suggests flake.")
    elif base_label == "Regressing":
        score["regression"] += 0.5; reasons.append("Consistent failures suggest regression.")
    # metrics signal
    if pass_rate >= 90.0:
        score["flake"] += 0.2
    elif pass_rate <= 50.0 and runs_total >= 10:
        score["regression"] += 0.2
    # logs signal
    if any(any(pat in s.lower() for pat in INFRA_PATTERNS) for s in log_snips):
        score["infra"] += 0.6; reasons.append("CI logs match infra-like patterns (timeouts/network).")

    label = max(score, key=score.get).title()
    flaky = (label == "Flake")
    if label == "Flake":
        label = "Flaky"

    t_ms = (time.perf_counter() - t0) * 1000.0
    audit_write("classify_aggregate", req.model_dump(),
                {"label": label, "score": score, "reasons": reasons}, ok=True, t_ms=t_ms)

    return ClassifyAggregateResponse(
        label=label, flaky=flaky, score={k: round(v, 3) for k, v in score.items()},
        failures=failures, runs=runs,
        evidence=Evidence(pass_rate=pass_rate, runs_total=runs_total, log_snippets=log_snips),
        reasons=reasons,
    )

# ── tool registry for mcp.list_tools ─────────────────────────────────────────
TOOLS: Dict[str, Dict[str, Any]] = {
    "is_flaky": {
        "input_schema": FlakyRequest.model_json_schema(),
        "output_schema": FlakyResponse.model_json_schema(),
        "description": "Classify a test's flakiness from pass/fail history.",
    },
    "suggest_fix": {
        "input_schema": SuggestFixRequest.model_json_schema(),
        "output_schema": SuggestFixResponse.model_json_schema(),
        "description": "Suggest deterministic fixes for a flaky test.",
    },
    "get_actions_metrics": {
        "input_schema": ActionsMetricsRequest.model_json_schema(),
        "output_schema": ActionsMetricsResponse.model_json_schema(),
        "description": "Summarize GitHub Actions pass/fail metrics for a repo/branch.",
    },
    "get_ci_log_snippets": {
        "input_schema": GetLogSnippetsRequest.model_json_schema(),
        "output_schema": GetLogSnippetsResponse.model_json_schema(),
        "description": "Fetch failure snippets from a GitHub Actions run's logs.",
    },
    "open_pr": {
        "input_schema": OpenPRRequest.model_json_schema(),
        "output_schema": OpenPRResponse.model_json_schema(),
        "description": "Open a draft PR with context for human review.",
    },
    "create_jira": {
        "input_schema": CreateJiraRequest.model_json_schema(),
        "output_schema": CreateJiraResponse.model_json_schema(),
        "description": "Create a Jira ticket for flaky test tracking.",
    },
    "classify_aggregate": {
        "input_schema": ClassifyAggregateRequest.model_json_schema(),
        "output_schema": ClassifyAggregateResponse.model_json_schema(),
        "description": "Aggregate history + Actions metrics + CI logs to reduce false alarms.",
    },
}

def _list_tools() -> Dict[str, Any]:
    return {
        "transport": "stdio",
        "tools": [
            {"name": name, "description": meta["description"],
             "input": meta["input_schema"], "output": meta["output_schema"]}
            for name, meta in TOOLS.items()
        ],
    }

# ── JSON-RPC plumbing ────────────────────────────────────────────────────────
def _ok(id_: Any, result: Any) -> Dict[str, Any]:
    return {"jsonrpc": JSONRPC_VERSION, "id": id_, "result": result}

def _err(id_: Any, code: int, message: str, data: Optional[Any] = None) -> Dict[str, Any]:
    e = {"code": code, "message": message}
    if data is not None: e["data"] = data
    return {"jsonrpc": JSONRPC_VERSION, "id": id_, "error": e}

def handle(req: Dict[str, Any]) -> Dict[str, Any]:
    if req.get("jsonrpc") != JSONRPC_VERSION:
        return _err(req.get("id"), -32600, "Invalid Request: jsonrpc must be '2.0'")
    method = req.get("method"); id_ = req.get("id"); params = req.get("params") or {}
    try:
        if method == "mcp.list_tools":
            return _ok(id_, _list_tools())
        if method == "is_flaky":
            return _ok(id_, tool_is_flaky(FlakyRequest(**params)).model_dump())
        if method == "suggest_fix":
            return _ok(id_, tool_suggest_fix(SuggestFixRequest(**params)).model_dump())
        if method == "get_actions_metrics":
            return _ok(id_, tool_get_actions_metrics(ActionsMetricsRequest(**params)).model_dump())
        if method == "get_ci_log_snippets":
            return _ok(id_, tool_get_ci_log_snippets(GetLogSnippetsRequest(**params)).model_dump())
        if method == "open_pr":
            return _ok(id_, tool_open_pr(OpenPRRequest(**params)).model_dump())
        if method == "create_jira":
            return _ok(id_, tool_create_jira(CreateJiraRequest(**params)).model_dump())
        if method == "classify_aggregate":
            return _ok(id_, tool_classify_aggregate(ClassifyAggregateRequest(**params)).model_dump())
        return _err(id_, -32601, f"Method not found: {method}")
    except Exception as e:
        tb = traceback.format_exc()
        audit_write("exception", {"method": method, "params": params}, {"error": str(e)}, ok=False, t_ms=0.0)
        return _err(id_, -32603, f"Internal error: {e}", data=tb)

def main():
    for line in sys.stdin:
        line = line.strip()
        if not line: continue
        try:
            req = json.loads(line)
            resp = handle(req)
        except Exception as e:
            resp = _err(None, -32700, f"Parse error: {e}")
        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()

if __name__ == "__main__":
    main()

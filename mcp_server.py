#!/usr/bin/env python3
import sys, json, traceback
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

JSONRPC_VERSION = "2.0"

# ── Schemas ────────────────────────────────────────────────────
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

# ── Tool logic ─────────────────────────────────────────────────
def classify(history: List[str]) -> Dict[str, Any]:
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

def is_flaky(req: FlakyRequest) -> FlakyResponse:
    res = classify(req.history)
    return FlakyResponse(
        flaky=res["flaky"],
        failures=res["failures"],
        runs=res["runs"],
        label=res["label"],
    )

def suggest_fix(req: SuggestFixRequest) -> SuggestFixResponse:
    h = [s.strip().lower() for s in req.history if s.strip()]
    tips = set()
    if "fail" in h and "pass" in h:
        tips.add("Seed all RNG; replace time.sleep with condition-based waits.")
        tips.add("Freeze time (freezegun/fake timers) to eliminate clock drift.")
    tips.add("Mock external deps (network/files/db) to remove nondeterminism.")
    tips.add("Ensure test order independence; isolate global state and I/O.")
    return SuggestFixResponse(suggestions=sorted(tips))

# ── MCP metadata / discovery ───────────────────────────────────
TOOLS = {
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
}

def list_tools_result() -> Dict[str, Any]:
    return {
        "transport": "stdio",
        "tools": [
            {
                "name": name,
                "description": meta["description"],
                "input": meta["input_schema"],
                "output": meta["output_schema"],
            }
            for name, meta in TOOLS.items()
        ],
    }

# ── JSON-RPC helpers ──────────────────────────────────────────
def make_result(id_: Any, result: Any) -> Dict[str, Any]:
    return {"jsonrpc": JSONRPC_VERSION, "id": id_, "result": result}

def make_error(id_: Any, code: int, message: str, data: Optional[Any] = None) -> Dict[str, Any]:
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": JSONRPC_VERSION, "id": id_, "error": err}

def handle_request(req: Dict[str, Any]) -> Dict[str, Any]:
    if req.get("jsonrpc") != JSONRPC_VERSION:
        return make_error(req.get("id"), -32600, "Invalid Request: jsonrpc must be '2.0'")
    method = req.get("method")
    id_ = req.get("id")
    params = req.get("params") or {}

    try:
        if method == "mcp.list_tools":
            return make_result(id_, list_tools_result())
        elif method == "is_flaky":
            payload = FlakyRequest(**params)
            out = is_flaky(payload).model_dump()
            return make_result(id_, out)
        elif method == "suggest_fix":
            payload = SuggestFixRequest(**params)
            out = suggest_fix(payload).model_dump()
            return make_result(id_, out)
        else:
            return make_error(id_, -32601, f"Method not found: {method}")
    except Exception as e:
        tb = traceback.format_exc()
        return make_error(id_, -32603, f"Internal error: {e}", data=tb)

def main():
    # One JSON-RPC request per line on STDIN, one response per line on STDOUT.
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            resp = handle_request(req)
        except Exception as e:
            resp = make_error(None, -32700, f"Parse error: {e}")
        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()

if __name__ == "__main__":
    main()

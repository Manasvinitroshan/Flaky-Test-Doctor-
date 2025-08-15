#!/usr/bin/env python3
import os, json, subprocess
from typing import Any, Dict, Optional
from dotenv import load_dotenv; load_dotenv()

import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from prometheus_fastapi_instrumentator import Instrumentator

from adapters.jira_adapter import Jira
from adapters.github_adapter import GitHub

PYTHON_BIN = os.getenv("PYTHON", "python")
OPA_URL = os.getenv("OPA_URL")  # optional policy URL

app = FastAPI(title="Flaky Test Doctor • HTTP Facade")
Instrumentator().instrument(app).expose(app)  # /metrics

# serve /static for any future assets
if os.path.isdir("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")

def mcp_call(method: str, params: Dict[str,Any] | None=None) -> Dict[str,Any]:
    """Spawn stdio MCP server and make a single JSON-RPC call."""
    proc = subprocess.Popen([PYTHON_BIN, "mcp_server.py"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, bufsize=1)
    req = {"jsonrpc":"2.0","id":1,"method":method}
    if params is not None: req["params"] = params
    proc.stdin.write(json.dumps(req) + "\n"); proc.stdin.flush(); proc.stdin.close()
    line = proc.stdout.readline().strip()
    if not line:
        raise HTTPException(500, "Empty response from MCP server")
    data = json.loads(line)
    if "error" in data: raise HTTPException(500, str(data["error"]))
    return data["result"]

def opa_enforce(req: Request):
    if not OPA_URL: return
    try:
        resp = requests.post(OPA_URL, json={"input":{
            "method": req.method, "path": req.url.path, "headers": dict(req.headers)
        }}, timeout=5)
        allow = resp.json().get("result",{}).get("allow",False)
        if not allow: raise HTTPException(403, "Forbidden by policy")
    except requests.RequestException as e:
        raise HTTPException(500, f"OPA not reachable: {e}")

# ---------- UI ----------
@app.get("/", response_class=HTMLResponse)
def home():
    return """
<!doctype html><html><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Flaky Test Doctor (MCP)</title>
<style>body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;padding:24px;max-width:900px;margin:auto}
.card{border:1px solid #ddd;border-radius:12px;padding:16px;margin:12px 0}
input,textarea{width:100%;padding:8px;margin-top:6px}
button{padding:8px 12px;border-radius:8px;border:1px solid #444;background:#111;color:#fff;cursor:pointer}
pre{background:#fafafa;border:1px solid #eee;padding:12px;border-radius:8px;overflow:auto}
.grid{display:grid;gap:12px;grid-template-columns:1fr 1fr}
</style>
</head><body>
<h1>Flaky Test Doctor</h1>
<p>Run flakiness checks via MCP stdio through this façade.</p>

<div class="card">
  <h3>1) Quick check</h3>
  <div class="grid">
    <div>
      <label>Test name <input id="name" placeholder="suite or test_id"/></label>
    </div>
    <div>
      <label>History (JSON array) <input id="hist" value='["pass","fail","pass"]'/></label>
    </div>
  </div>
  <p><button onclick="callIsFlaky()">Check flakiness</button>
     <button onclick="callSuggest()">Suggest fix</button></p>
  <pre id="out"></pre>
</div>

<div class="card">
  <h3>2) Build history from pytest (local shell)</h3>
  <p>Run in your terminal, then paste the JSON above:</p>
  <pre>python -m pytest -q | tee pytest.out
python cli/pytest_to_history.py pytest.out > history.json
cat history.json</pre>
</div>

<script>
async function callIsFlaky(){
  const body = { test_name: document.getElementById('name').value || 'suite',
                 history: JSON.parse(document.getElementById('hist').value || '[]') };
  const r = await fetch('/is_flaky',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  document.getElementById('out').textContent = JSON.stringify(await r.json(), null, 2);
}
async function callSuggest(){
  const body = { test_name: document.getElementById('name').value || 'suite',
                 history: JSON.parse(document.getElementById('hist').value || '[]') };
  const r = await fetch('/suggest_fix',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  document.getElementById('out').textContent = JSON.stringify(await r.json(), null, 2);
}
</script>
</body></html>
"""

# ---------- JSON models ----------
class FlakyBody(BaseModel):
    test_name: str
    history: list[str]

class JiraCreateBody(BaseModel):
    project_key: str
    summary: str
    description: str = ""
    issue_type: str = "Task"

class OpenPRBody(BaseModel):
    repo: str
    head: str
    base: str
    title: str
    body: str = ""
    draft: bool = True

# ---------- MCP-backed endpoints ----------
@app.post("/is_flaky")
def is_flaky(body: FlakyBody, request: Request):
    return mcp_call("is_flaky", body.model_dump())

@app.post("/suggest_fix")
def suggest_fix(body: FlakyBody, request: Request):
    return mcp_call("suggest_fix", body.model_dump())

# ---------- Optional: direct adapters for resume wow-factor ----------
@app.post("/create_jira")
def create_jira(body: JiraCreateBody, request: Request):
    opa_enforce(request)
    # Read credentials from env for safety
    j = Jira()
    out = j.create_issue(body.project_key, body.summary, body.description, body.issue_type)
    return {"key": out["key"], "id": out["id"]}

@app.post("/open_pr")
def open_pr(body: OpenPRBody, request: Request):
    opa_enforce(request)
    gh = GitHub()
    pr = gh.open_pr(body.repo, body.head, body.base, body.title, body.body, body.draft)
    return pr.model_dump()

#!/usr/bin/env python3
import os, json, subprocess
from typing import Any, Dict, Optional, List
from dotenv import load_dotenv; load_dotenv()

import requests
from fastapi import FastAPI, HTTPException, Request, Query
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from prometheus_fastapi_instrumentator import Instrumentator

from adapters.jira_adapter import Jira
from adapters.github_adapter import GitHub

PYTHON_BIN = os.getenv("PYTHON", "python")
OPA_URL     = os.getenv("OPA_URL")  # optional OPA allow/deny URL
GITHUB_API  = os.getenv("GITHUB_API", "https://api.github.com")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")

app = FastAPI(title="Flaky Test Doctor • HTTP Facade")
Instrumentator().instrument(app).expose(app)  # /metrics

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

# ---------- GitHub repo listing (dynamic) ----------
def _gh_session() -> requests.Session:
    s = requests.Session()
    if not GITHUB_TOKEN:
        raise HTTPException(500, "GITHUB_TOKEN not set on server")
    s.headers.update({
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    })
    return s

def _collect_repos(per_page: int, page: int) -> List[Dict[str,Any]]:
    """Return repos visible to the token (user repos + memberships)."""
    s = _gh_session()
    # /user/repos covers repos the authenticated user has explicit access to.
    # Add affiliation to include owner, collaborator, organization-member.
    params = {"per_page": per_page, "page": page, "affiliation": "owner,collaborator,organization_member", "sort": "pushed"}
    r = s.get(f"{GITHUB_API}/user/repos", params=params, timeout=15)
    if r.status_code == 401:
        raise HTTPException(401, "GitHub token unauthorized")
    r.raise_for_status()
    return r.json()

@app.get("/repos")
def list_repos(q: Optional[str] = Query(None, description="substring filter (case-insensitive)"),
               per_page: int = Query(100, ge=1, le=100),
               page: int = Query(1, ge=1)):
    """
    Returns a list of repo slugs (owner/repo) visible to the server token.
    Optional substring filter ?q=foo; supports pagination.
    """
    try:
        js = _collect_repos(per_page=per_page, page=page)
    except requests.RequestException as e:
        raise HTTPException(502, f"GitHub fetch failed: {e}")
    names = [f"{r['owner']['login']}/{r['name']}" for r in js if 'owner' in r and 'name' in r]
    if q:
        qlow = q.lower()
        names = [n for n in names if qlow in n.lower()]
    return {"page": page, "per_page": per_page, "count": len(names), "items": names}

# ---------- UI ----------
@app.get("/", response_class=HTMLResponse)
def home():
    return """
<!doctype html><html><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Flaky Test Doctor (MCP)</title>
<style>
body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;padding:24px;max-width:1100px;margin:auto}
.card{border:1px solid #ddd;border-radius:12px;padding:16px;margin:12px 0}
input,textarea,button,select{padding:8px;margin-top:6px}
input,textarea,select{width:100%}
button{border-radius:8px;border:1px solid #444;background:#111;color:#fff;cursor:pointer}
pre{background:#fafafa;border:1px solid #eee;padding:12px;border-radius:8px;overflow:auto;max-height:340px}
.grid{display:grid;gap:12px;grid-template-columns:1fr 1fr}
.two{display:grid;gap:12px;grid-template-columns:2fr 1fr}
.small{font-size:12px;color:#666}
</style>
</head><body>
<h1>Flaky Test Doctor</h1>
<p class="small">Backed by MCP (stdio). Pick a repo via live GitHub API (token on server), then run checks.</p>

<div class="card">
  <h3>1) Quick Local Check</h3>
  <div class="grid">
    <div><label>Test name <input id="name" placeholder="suite or test_id"/></label></div>
    <div><label>History (JSON array) <input id="hist" value='["pass","fail","pass"]'/></label></div>
  </div>
  <p>
    <button onclick="callIsFlaky()">Check flakiness</button>
    <button onclick="callSuggest()">Suggest fix</button>
  </p>
  <pre id="out1"></pre>
</div>

<div class="card">
  <h3>2) Repo-Aware Checks (GitHub)</h3>
  <div class="grid">
    <div>
      <label>Repository (owner/repo, autocomplete from your GitHub)
        <input id="repo" list="repoList" placeholder="owner/repo"/>
        <datalist id="repoList"></datalist>
      </label>
      <div class="small">Tip: type to filter; “Refresh list” loads again from GitHub.</div>
      <p>
        <button onclick="loadRepos()">Refresh list</button>
        <input id="filter" placeholder="filter (optional)" style="width:200px"/>
      </p>
    </div>
    <div>
      <label>Run ID (optional for classify; required for log snippets)
        <input id="runId" placeholder="e.g. 1234567890"/>
      </label>
    </div>
  </div>

  <div class="grid">
    <div><label>Test name <input id="name2" placeholder="login_test"/></label></div>
    <div><label>History (optional JSON array) <input id="hist2" placeholder='["pass","fail","pass","fail"]'/></label></div>
  </div>

  <p>
    <button onclick="callActionsMetrics()">Get Actions metrics</button>
    <button onclick="callLogSnippets()">Get CI log snippets</button>
    <button onclick="callClassifyAggregate()">Classify (aggregate)</button>
  </p>
  <pre id="out2"></pre>
</div>

<div class="card">
  <h3>3) Build history from pytest (local shell)</h3>
  <p>Run in your terminal, then paste the JSON into the History field above:</p>
  <pre>python -m pytest -q | tee pytest.out
python cli/pytest_to_history.py pytest.out > history.json
cat history.json</pre>
</div>

<script>
async function loadRepos(page=1){
  const q = document.getElementById('filter').value.trim();
  const url = new URL('/repos', window.location.origin);
  url.searchParams.set('per_page','100');
  url.searchParams.set('page', String(page));
  if(q) url.searchParams.set('q', q);
  const r = await fetch(url.toString());
  const js = await r.json();
  const dl = document.getElementById('repoList');
  dl.innerHTML = '';
  (js.items || []).forEach(n => {
    const o = document.createElement('option');
    o.value = n; dl.appendChild(o);
  });
}

function getVal(id){ return document.getElementById(id).value }
function getJSON(id){
  const v = getVal(id).trim(); if(!v) return null;
  try { return JSON.parse(v) } catch(e){ alert("Invalid JSON in "+id); throw e; }
}

async function post(path, body){
  const r = await fetch(path, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  const t = await r.text(); try { return JSON.parse(t) } catch { return {raw:t} }
}

async function callIsFlaky(){
  const body = { test_name: getVal('name') || 'suite',
                 history: getJSON('hist') || [] };
  const res = await post('/is_flaky', body);
  document.getElementById('out1').textContent = JSON.stringify(res, null, 2);
}
async function callSuggest(){
  const body = { test_name: getVal('name') || 'suite',
                 history: getJSON('hist') || [] };
  const res = await post('/suggest_fix', body);
  document.getElementById('out1').textContent = JSON.stringify(res, null, 2);
}

async function callActionsMetrics(){
  const repo = getVal('repo').trim();
  if(!repo) return alert('Enter repo as owner/repo');
  const res = await post('/get_actions_metrics', {repo});
  document.getElementById('out2').textContent = JSON.stringify(res, null, 2);
}

async function callLogSnippets(){
  const repo = getVal('repo').trim();
  const run_id = parseInt(getVal('runId')||"", 10);
  if(!repo) return alert('Enter repo as owner/repo');
  if(!run_id) return alert('Enter a numeric run id');
  const res = await post('/get_ci_log_snippets', {repo, run_id, max_snippets: 20});
  document.getElementById('out2').textContent = JSON.stringify(res, null, 2);
}

async function callClassifyAggregate(){
  const repo = getVal('repo').trim() || null;
  const run_id = getVal('runId').trim() ? parseInt(getVal('runId'),10) : null;
  const test_name = getVal('name2') || 'suite';
  const hist = getJSON('hist2');
  const res = await post('/classify_aggregate', {test_name, repo, run_id, history: hist, max_log_snippets: 20});
  document.getElementById('out2').textContent = JSON.stringify(res, null, 2);
}

// auto-load on page open
window.addEventListener('DOMContentLoaded', () => loadRepos());
</script>
</body></html>
"""

# ---------- JSON bodies ----------
class FlakyBody(BaseModel):
    test_name: str
    history: List[str]

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

class ActionsMetricsBody(BaseModel):
    repo: str
    branch: Optional[str] = None

class LogSnippetsBody(BaseModel):
    repo: str
    run_id: int
    max_files: int = 10
    max_snippets: int = 20

class ClassifyAggregateBody(BaseModel):
    test_name: str
    repo: Optional[str] = None
    run_id: Optional[int] = None
    history: Optional[List[str]] = None
    max_log_snippets: int = 20

# ---------- MCP-backed endpoints ----------
@app.post("/is_flaky")
def is_flaky(body: FlakyBody, request: Request):
    return mcp_call("is_flaky", body.model_dump())

@app.post("/suggest_fix")
def suggest_fix(body: FlakyBody, request: Request):
    return mcp_call("suggest_fix", body.model_dump())

@app.post("/get_actions_metrics")
def get_actions_metrics(body: ActionsMetricsBody, request: Request):
    return mcp_call("get_actions_metrics", body.model_dump())

@app.post("/get_ci_log_snippets")
def get_ci_log_snippets(body: LogSnippetsBody, request: Request):
    return mcp_call("get_ci_log_snippets", body.model_dump())

@app.post("/classify_aggregate")
def classify_aggregate(body: ClassifyAggregateBody, request: Request):
    return mcp_call("classify_aggregate", body.model_dump())

# ---------- Optional: direct adapters ----------
@app.post("/create_jira")
def create_jira(body: JiraCreateBody, request: Request):
    opa_enforce(request)
    j = Jira()  # env: JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN
    out = j.create_issue(body.project_key, body.summary, body.description, body.issue_type)
    return {"key": out["key"], "id": out["id"]}

@app.post("/open_pr")
def open_pr(body: OpenPRBody, request: Request):
    opa_enforce(request)
    gh = GitHub()  # env: GITHUB_TOKEN
    pr = gh.open_pr(body.repo, body.head, body.base, body.title, body.body, body.draft)
    return pr.model_dump()

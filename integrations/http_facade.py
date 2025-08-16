#!/usr/bin/env python3
import os, json, subprocess, tempfile, shutil, pathlib, re, textwrap
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

PYTHON_BIN   = os.getenv("PYTHON", "python")
OPA_URL      = os.getenv("OPA_URL")
GITHUB_API   = os.getenv("GITHUB_API", "https://api.github.com")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")

ROOT_DIR = pathlib.Path(__file__).resolve().parent
RUNS = 5

app = FastAPI(title="Flaky Test Doctor • HTTP Facade")
Instrumentator().instrument(app).expose(app)

if os.path.isdir("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")

# ---------------- MCP stdio bridge ----------------
def mcp_call(method: str, params: Dict[str, Any] | None = None) -> Dict[str, Any]:
    proc = subprocess.Popen(
        [PYTHON_BIN, "mcp_server.py"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1,
    )
    req = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None: req["params"] = params
    proc.stdin.write(json.dumps(req) + "\n"); proc.stdin.flush(); proc.stdin.close()
    line = proc.stdout.readline().strip()
    if not line: raise HTTPException(500, "Empty response from MCP server")
    data = json.loads(line)
    if "error" in data: raise HTTPException(500, str(data["error"]))
    return data["result"]

# ---------------- OPA policy (optional) ----------------
def opa_enforce(req: Request):
    if not OPA_URL: return
    try:
        resp = requests.post(
            OPA_URL, json={"input":{"method":req.method,"path":req.url.path,"headers":dict(req.headers)}}, timeout=5
        )
        allow = resp.json().get("result",{}).get("allow",False)
        if not allow: raise HTTPException(403, "Forbidden by policy")
    except requests.RequestException as e:
        raise HTTPException(500, f"OPA not reachable: {e}")

# ---------------- GitHub repo listing (dynamic) ----------------
def _gh_session() -> requests.Session:
    s = requests.Session()
    if not GITHUB_TOKEN: raise HTTPException(500, "GITHUB_TOKEN not set on server")
    s.headers.update({
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    return s

def _collect_repos(per_page: int, page: int) -> List[Dict[str, Any]]:
    s = _gh_session()
    params = {"per_page": per_page, "page": page, "affiliation": "owner,collaborator,organization_member", "sort":"pushed"}
    r = s.get(f"{GITHUB_API}/user/repos", params=params, timeout=15)
    if r.status_code == 401: raise HTTPException(401, "GitHub token unauthorized")
    r.raise_for_status()
    return r.json()

@app.get("/repos")
def list_repos(q: Optional[str]=Query(None), per_page: int=Query(100, ge=1, le=100), page: int=Query(1, ge=1)):
    try:
        js = _collect_repos(per_page=per_page, page=page)
    except requests.RequestException as e:
        raise HTTPException(502, f"GitHub fetch failed: {e}")
    names = [f"{r['owner']['login']}/{r['name']}" for r in js if "owner" in r and "name" in r]
    if q:
        ql = q.lower()
        names = [n for n in names if ql in n.lower()]
    return {"page": page, "per_page": per_page, "count": len(names), "items": names}

# ---------------- helpers ----------------

ANSI_RE = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")  # robust ANSI CSI matcher

def _strip_ansi(s: str) -> str:
    return ANSI_RE.sub("", s)

    

def _run(cmd: List[str], cwd: Optional[str]=None, timeout: int=180) -> tuple[int, str]:
    p = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    out, _ = p.communicate(timeout=timeout)
    return p.returncode, out

def _ensure_pytest(cwd: Optional[str]=None):
    code, _ = _run([PYTHON_BIN, "-c", "import pytest,sys;sys.stdout.write(getattr(pytest,'__version__',''))"], cwd=cwd, timeout=30)
    if code != 0:
        _run([PYTHON_BIN, "-m", "pip", "install", "pytest"], cwd=cwd, timeout=240)

def _pytest_verbose_to_history(text: str) -> List[str]:
    """
    Parse per-test lines in verbose output to preserve order, e.g.:
    tests/test_auto_flaky.py::test_auto_flaky[0] PASSED
    tests/test_auto_flaky.py::test_auto_flaky[1] FAILED
    """
    hist: List[str] = []
    for line in _strip_ansi(text).splitlines():
        line = line.strip()
        if not line: continue
        if re.search(r"\bPASSED\b", line): hist.append("pass")
        elif re.search(r"\bFAILED\b", line): hist.append("fail")
    return hist

def _pytest_summary_to_history(text: str) -> List[str]:
    passed = failed = 0
    t = _strip_ansi(text)
    m = re.search(r"(\d+)\s+passed", t);  passed = int(m.group(1)) if m else 0
    m = re.search(r"(\d+)\s+failed", t);  failed = int(m.group(1)) if m else 0
    return (["pass"]*passed)+(["fail"]*failed)

def _write_auto_test(tmp_repo_root: str, rel_file: str, func: str) -> pathlib.Path:
    tests_dir = pathlib.Path(tmp_repo_root, "tests"); tests_dir.mkdir(parents=True, exist_ok=True)
    test_file = tests_dir / "test_auto_flaky.py"
    content = f"""# Auto-generated by Flaky Test Doctor
import importlib.util, pathlib, pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
TARGET = ROOT / {rel_file!r}

spec = importlib.util.spec_from_file_location("target_mod", TARGET)
mod = importlib.util.module_from_spec(spec)  # type: ignore
spec.loader.exec_module(mod)  # type: ignore

@pytest.mark.parametrize("i", list(range({RUNS})))
def test_auto_flaky(i):
    out = getattr(mod, {func!r})(i)
    assert isinstance(out, str) and out.startswith("processed-")
"""
    test_file.write_text(content, encoding="utf-8")
    return test_file

def _direct_harness_history(tmp_repo_root: str, rel_file: str, func: str, attempts: int=RUNS) -> List[str]:
    script = textwrap.dedent(f"""
        import importlib.util, pathlib, json, sys
        ROOT = pathlib.Path({repr(tmp_repo_root)})
        TARGET = ROOT / {rel_file!r}
        spec = importlib.util.spec_from_file_location("target_mod", TARGET)
        mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)  # type: ignore
        hist = []; fn = getattr(mod, {func!r})
        for i in range({attempts}):
            try:
                out = fn(i)
                hist.append("pass" if (isinstance(out,str) and out.startswith("processed-")) else "fail")
            except Exception:
                hist.append("fail")
        sys.stdout.write(json.dumps(hist))
    """).strip()
    tmp_py = pathlib.Path(tmp_repo_root, "_ftd_harness.py")
    tmp_py.write_text(script, encoding="utf-8")
    code, out = _run([PYTHON_BIN, str(tmp_py)], cwd=tmp_repo_root, timeout=60)
    try: return json.loads(out.strip()) if code == 0 else []
    except Exception: return []

def _analyze_code_for_flakiness(code: str) -> List[str]:
    """Very lightweight static hints pulled from the target file."""
    hints: List[str] = []
    def add(s): 
        if s not in hints: hints.append(s)
    t = code

    if re.search(r"\brandom\.", t) and not re.search(r"\brandom\.seed\(", t):
        add("Seed the RNG (e.g., random.seed(0)) or inject a deterministic source.")
    if re.search(r"\btime\.sleep\(", t):
        add("Avoid time.sleep in tests; prefer deterministic waits or fake timers.")
    if re.search(r"\b(datetime|time)\.(now|time|localtime)\(", t):
        add("Mock time/date to prevent time-based nondeterminism.")
    if re.search(r"\brequests\.", t) or re.search(r"\burllib\.", t):
        add("Mock network I/O; tests should not hit real services.")
    if re.search(r"\bos\.environ\[\s*['\"]", t):
        add("Stabilize env-dependent behavior; set env vars explicitly in tests.")
    if re.search(r"\bsubprocess\.", t):
        add("Mock subprocess calls or assert on faked outputs.")
    if re.search(r"\bthreading\.|multiprocessing\.", t):
        add("Synchronize threads/processes or isolate shared state.")
    if re.search(r"\bopen\(.+['\"][wa]\b", t):
        add("Isolate filesystem writes (tmpdir/monkeypatch) to avoid cross-test interference.")
    return hints

# ---------------- UI ----------------
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
    <div><label>History (optional JSON array) <input id="hist2" placeholder='[]'/></label></div>
  </div>

  <div class="grid">
    <div><label>File path (optional, for auto-test) <input id="filePath" placeholder="one.py"/></label></div>
    <div><label>Function (optional, for auto-test) <input id="funcName" placeholder="flaky_function"/></label></div>
  </div>

  <p>
    <button onclick="runPytest()">Run pytest → build history</button>
    <button onclick="suggestFixRepo()">Suggest fix (from repo history)</button>
  </p>

  <p>
    <button onclick="callActionsMetrics()">Get Actions metrics</button>
    <button onclick="callLogSnippets()">Get CI log snippets</button>
    <button onclick="callClassifyAggregate()">Classify (aggregate)</button>
  </p>
  <pre id="out2"></pre>
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
  (js.items || []).forEach(n => { const o = document.createElement('option'); o.value = n; dl.appendChild(o); });
}

function getVal(id){ return document.getElementById(id).value }
function getJSON(id){ const v = getVal(id).trim(); if(!v) return null;
  try { return JSON.parse(v) } catch(e){ alert("Invalid JSON in "+id); throw e; } }

async function post(path, body){
  const r = await fetch(path, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  const t = await r.text(); try { return JSON.parse(t) } catch { return {raw:t} }
}

async function callIsFlaky(){
  const body = { test_name: getVal('name') || 'suite', history: getJSON('hist') || [] };
  const res = await post('/is_flaky', body);
  document.getElementById('out1').textContent = JSON.stringify(res, null, 2);
}
async function callSuggest(){
  const body = { test_name: getVal('name') || 'suite', history: getJSON('hist') || [] };
  const res = await post('/suggest_fix', body);
  document.getElementById('out1').textContent = JSON.stringify(res, null, 2);
}

// Append history instead of overwriting
function _appendHistoryField(fieldId, newHist){
  const existing = getJSON(fieldId) || [];
  document.getElementById(fieldId).value = JSON.stringify(existing.concat(newHist));
}

async function runPytest(){
  const repo = getVal('repo').trim(); if(!repo) return alert('Enter repo as owner/repo');
  const res = await post('/run_pytest', {
    repo, file_path: getVal('filePath') || undefined, func_name: getVal('funcName') || undefined
  });
  document.getElementById('out2').textContent = JSON.stringify(res, null, 2);
  if(res && res.history) _appendHistoryField('hist2', res.history);
}
async function suggestFixRepo(){
  const repo = getVal('repo').trim(); if(!repo) return alert('Enter repo as owner/repo');
  const test_name = getVal('name2') || 'suite';
  const res = await post('/suggest_fix_repo', {
    repo, test_name, file_path: getVal('filePath') || undefined, func_name: getVal('funcName') || undefined
  });
  document.getElementById('out2').textContent = JSON.stringify(res, null, 2);
  if(res && res.history) _appendHistoryField('hist2', res.history);
}

async function callActionsMetrics(){
  const repo = getVal('repo').trim(); if(!repo) return alert('Enter repo as owner/repo');
  const res = await post('/get_actions_metrics', {repo});
  document.getElementById('out2').textContent = JSON.stringify(res, null, 2);
}
async function callLogSnippets(){
  const repo = getVal('repo').trim(); const run_id = parseInt(getVal('runId')||"", 10);
  if(!repo) return alert('Enter repo as owner/repo'); if(!run_id) return alert('Enter a numeric run id');
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

window.addEventListener('DOMContentLoaded', () => loadRepos());
</script>
</body></html>
"""

# ---------------- JSON bodies ----------------
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

class RunPytestBody(BaseModel):
    repo: str
    ref: Optional[str] = None
    file_path: Optional[str] = None
    func_name: Optional[str] = None

class SuggestFixRepoBody(BaseModel):
    repo: str
    test_name: str = "suite"
    ref: Optional[str] = None
    file_path: Optional[str] = None
    func_name: Optional[str] = None

# ---------------- MCP-backed endpoints ----------------
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

# ---------------- Optional: direct adapters ----------------
@app.post("/create_jira")
def create_jira(body: JiraCreateBody, request: Request):
    opa_enforce(request)
    j = Jira()
    out = j.create_issue(body.project_key, body.summary, body.description, body.issue_type)
    return {"key": out["key"], "id": out["id"]}

@app.post("/open_pr")
def open_pr(body: OpenPRBody, request: Request):
    opa_enforce(request)
    gh = GitHub()
    pr = gh.open_pr(body.repo, body.head, body.base, body.title, body.body, body.draft)
    return pr.model_dump()

# ---------------- clone + run pytest + build history ----------------
@app.post("/run_pytest")
def run_pytest_endpoint(body: RunPytestBody):
    """
    Clone repo, ensure pytest, run tests in verbose mode to capture per-test order.
    If no tests found and file_path+func_name provided, inject a parametrized test with RUNS cases
    and re-run; if still nothing, use a direct harness to produce history.
    Returns {history, raw, exit_code, code_excerpt?}
    """
    repo = (body.repo or "").strip()
    ref  = (body.ref  or "").strip()
    if not repo or "/" not in repo:
        raise HTTPException(400, "repo must be 'owner/name'")

    tmp = tempfile.mkdtemp(prefix="ftd_")
    try:
        url = f"https://github.com/{repo}.git"
        code, out = _run(["git", "clone", "--depth=1", url, tmp], timeout=180)
        if code != 0: raise HTTPException(502, f"git clone failed:\n{out}")
        if ref:
            code, out = _run(["git", "-C", tmp, "checkout", ref], timeout=120)
            if code != 0: raise HTTPException(400, f"git checkout '{ref}' failed:\n{out}")

        req = pathlib.Path(tmp, "requirements.txt")
        if req.exists():
            _run([PYTHON_BIN, "-m", "pip", "install", "-r", str(req)], cwd=tmp, timeout=240)
        _ensure_pytest(tmp)

        # 1) normal discovery (verbose, show all results)
        cmd = [PYTHON_BIN, "-m", "pytest", "-vv", "-rA", "--disable-warnings", "--maxfail=1000000"]
        code, test_out = _run(cmd, cwd=tmp, timeout=300)
        hist = _pytest_verbose_to_history(test_out) or _pytest_summary_to_history(test_out)

        code_excerpt = None
        if body.file_path:
            target = pathlib.Path(tmp, body.file_path)
            if target.exists():
                code_excerpt = target.read_text(encoding="utf-8", errors="ignore")[:4000]

        # 2) if we still don’t have multiple ordered results and we have target → inject auto-test
        if (not hist) and body.file_path and body.func_name:
            generated = _write_auto_test(tmp, body.file_path, body.func_name)
            code, test_out = _run([PYTHON_BIN, "-m", "pytest", "-vv", "-rA", str(generated)], cwd=tmp, timeout=300)
            hist = _pytest_verbose_to_history(test_out) or _pytest_summary_to_history(test_out)
            if not hist:
                hist = _direct_harness_history(tmp, body.file_path, body.func_name, attempts=RUNS)

        return {"history": hist, "raw": test_out, "exit_code": code, "code_excerpt": code_excerpt}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

# ---------------- suggest fix using repo-derived history + code ----------------
@app.post("/suggest_fix_repo")
def suggest_fix_repo(body: SuggestFixRepoBody):
    rp = RunPytestBody(repo=body.repo, ref=body.ref, file_path=body.file_path, func_name=body.func_name)
    rp_res = run_pytest_endpoint(rp)
    history = rp_res.get("history") or []
    code_excerpt = rp_res.get("code_excerpt") or ""
    # 1) LLM / MCP suggestions from history
    llm = []
    try:
        llm = mcp_call("suggest_fix", {"test_name": body.test_name, "history": history}).get("suggestions", [])
    except HTTPException:
        llm = []
    # 2) Static code hints from the target file (if available)
    heur = _analyze_code_for_flakiness(code_excerpt) if code_excerpt else []
    # Merge and dedupe while preserving order
    seen = set(); merged = []
    for s in (llm + heur):
        if s not in seen:
            merged.append(s); seen.add(s)
    return {"history": history, "suggestions": merged}

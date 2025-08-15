#!/usr/bin/env python3
import os, json, subprocess, tempfile, zipfile, io
from typing import Dict, Any
import requests
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel
from prometheus_fastapi_instrumentator import Instrumentator

# ---------- MCP stdio bridge ----------
def mcp_call(method: str, params: Dict[str, Any] | None = None, timeout: int = 30):
    proc = subprocess.Popen(
        [os.getenv("PYTHON", "python"), "mcp_server.py"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1
    )
    req = {"jsonrpc":"2.0","id":1,"method":method}
    if params is not None:
        req["params"] = params
    proc.stdin.write(json.dumps(req) + "\n"); proc.stdin.flush()
    proc.stdin.close()
    line = proc.stdout.readline().strip()
    try:
        data = json.loads(line)
    except Exception:
        raise HTTPException(500, f"Invalid MCP response: {line}")
    if "error" in data:
        raise HTTPException(500, f"MCP error: {data['error']}")
    return data["result"]

# ---------- OPA policy check ----------
OPA_URL = os.getenv("OPA_URL")  # e.g., http://localhost:8181/v1/data/mcp/authz
def opa_enforce(request: Request):
    if not OPA_URL:
        return  # allow when not configured
    headers = dict(request.headers)
    payload = {"input": {"method": request.method, "path": request.url.path, "headers": headers}}
    try:
        resp = requests.post(OPA_URL, json=payload, timeout=5)
        allow = resp.json().get("result", {}).get("allow", False)
        if not allow:
            raise HTTPException(status_code=403, detail="Forbidden by policy")
    except requests.RequestException as e:
        raise HTTPException(500, f"OPA not reachable: {e}")

# ---------- FastAPI app ----------
app = FastAPI(title="MCP Facade")
Instrumentator().instrument(app).expose(app)  # /metrics

class FlakyBody(BaseModel):
    test_name: str
    history: list[str]

@app.post("/is_flaky")
def is_flaky(body: FlakyBody, request: Request):
    return mcp_call("is_flaky", body.model_dump())

@app.post("/suggest_fix")
def suggest_fix(body: FlakyBody, request: Request):
    return mcp_call("suggest_fix", body.model_dump())

# ---------- Adapters: GitHub PR / CI logs / Jira ----------
class OpenPRBody(BaseModel):
    repo: str           # e.g., "owner/repo"
    base: str           # e.g., "main"
    head: str           # e.g., "feature-branch"
    title: str
    body: str = ""

@app.post("/open_pr")
def open_pr(body: OpenPRBody, request: Request):
    opa_enforce(request)
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        raise HTTPException(400, "GITHUB_TOKEN not set")
    url = f"https://api.github.com/repos/{body.repo}/pulls"
    r = requests.post(url, headers={"Authorization": f"Bearer {token}",
                                    "Accept":"application/vnd.github+json"},
                      json=body.model_dump(), timeout=15)
    if r.status_code >= 300:
        raise HTTPException(r.status_code, r.text)
    return r.json()

class LogsBody(BaseModel):
    repo: str       # owner/repo
    run_id: int

@app.post("/fetch_ci_logs")
def fetch_ci_logs(body: LogsBody, request: Request):
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        raise HTTPException(400, "GITHUB_TOKEN not set")
    url = f"https://api.github.com/repos/{body.repo}/actions/runs/{body.run_id}/logs"
    r = requests.get(url, headers={"Authorization": f"Bearer {token}",
                                   "Accept":"application/vnd.github+json"}, timeout=20)
    if r.status_code >= 300:
        raise HTTPException(r.status_code, r.text)
    # GitHub returns a ZIP file; stream back basic info
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = z.namelist()
    # Optionally extract & search for failures here.
    return {"files": names[:20], "count": len(names)}

class JiraBody(BaseModel):
    base_url: str       # e.g., https://your-domain.atlassian.net
    email: str
    api_token: str
    project_key: str
    summary: str
    description: str = ""

@app.post("/create_jira")
def create_jira_issue(body: JiraBody, request: Request):
    opa_enforce(request)
    url = f"{body.base_url}/rest/api/3/issue"
    auth = (body.email, body.api_token)
    payload = {
        "fields": {
            "project": {"key": body.project_key},
            "summary": body.summary,
            "description": body.description,
            "issuetype": {"name": "Task"},
        }
    }
    r = requests.post(url, json=payload, auth=auth, timeout=20)
    if r.status_code >= 300:
        raise HTTPException(r.status_code, r.text)
    return r.json()

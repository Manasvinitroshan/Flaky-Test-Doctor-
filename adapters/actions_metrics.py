#!/usr/bin/env python3
"""
GitHub Actions metrics: summarize pass/fail for recent workflow runs.

Auth:
  GITHUB_TOKEN (read:org or repo scope)
"""

from __future__ import annotations
import os, datetime as dt
from typing import Any, Dict, List, Optional, Tuple
from pydantic import BaseModel
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

API = os.getenv("GITHUB_API", "https://api.github.com")
TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "20"))

def _session() -> requests.Session:
    s = requests.Session()
    retries = Retry(total=5, backoff_factor=0.2,
                    status_forcelist=[429, 500, 502, 503, 504],
                    allowed_methods=["GET"])
    s.mount("https://", HTTPAdapter(max_retries=retries))
    token = os.getenv("GITHUB_TOKEN")
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    s.headers.update(headers)
    return s

class RunSummary(BaseModel):
    id: int
    status: str
    conclusion: Optional[str]
    branch: str
    created_at: str

class Metrics(BaseModel):
    total: int
    passed: int
    failed: int
    pass_rate: float

class ActionsMetrics:
    def __init__(self, api_base: str = API):
        self.api = api_base
        self.s = _session()

    def list_runs(self, repo: str, branch: Optional[str] = None,
                  per_page: int = 30, max_pages: int = 3) -> List[RunSummary]:
        url = f"{self.api}/repos/{repo}/actions/runs"
        params = {"per_page": per_page}
        if branch:
            params["branch"] = branch
        runs: List[RunSummary] = []
        for _ in range(max_pages):
            r = self.s.get(url, params=params, timeout=TIMEOUT)
            r.raise_for_status()
            js = r.json()
            for run in js.get("workflow_runs", []):
                runs.append(RunSummary(
                    id=run["id"],
                    status=run.get("status", ""),
                    conclusion=run.get("conclusion"),
                    branch=run.get("head_branch", ""),
                    created_at=run.get("created_at", ""),
                ))
            if "next" not in (r.links or {}):
                break
            url = r.links["next"]["url"]
            params = None
        return runs

    def summarize(self, runs: List[RunSummary]) -> Metrics:
        total = len(runs)
        passed = sum(1 for r in runs if (r.conclusion or "").lower() == "success")
        failed = sum(1 for r in runs if (r.conclusion or "").lower() in {"failure", "cancelled", "timed_out"})
        rate = round(100.0 * passed / total, 2) if total else 0.0
        return Metrics(total=total, passed=passed, failed=failed, pass_rate=rate)

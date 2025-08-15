#!/usr/bin/env python3
"""
CI log store utilities:
 - Fetch a GitHub Actions run's logs (ZIP) and list files.
 - Extract failing chunks (best-effort heuristics).

Auth:
  GITHUB_TOKEN (repo read)
"""

from __future__ import annotations
import io, os, re, zipfile
from typing import Dict, List, Tuple
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

API = os.getenv("GITHUB_API", "https://api.github.com")
TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "30"))

def _session() -> requests.Session:
    s = requests.Session()
    retries = Retry(total=5, backoff_factor=0.3,
                    status_forcelist=[429, 500, 502, 503, 504],
                    allowed_methods=["GET"])
    s.mount("https://", HTTPAdapter(max_retries=retries))
    token = os.getenv("GITHUB_TOKEN")
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    s.headers.update(headers)
    return s

FAIL_PAT = re.compile(r"\b(FAIL|FAILED|ERROR|Traceback|AssertionError)\b", re.IGNORECASE)

class LogStore:
    def __init__(self, api_base: str = API):
        self.api = api_base
        self.s = _session()

    def fetch_run_logs_zip(self, repo: str, run_id: int) -> bytes:
        url = f"{self.api}/repos/{repo}/actions/runs/{run_id}/logs"
        r = self.s.get(url, timeout=TIMEOUT)
        r.raise_for_status()
        return r.content  # ZIP bytes

    def list_log_files(self, zip_bytes: bytes, limit: int = 50) -> List[str]:
        z = zipfile.ZipFile(io.BytesIO(zip_bytes))
        names = z.namelist()
        return names[:limit]

    def extract_failure_snippets(self, zip_bytes: bytes, max_files: int = 10, max_snippets: int = 20) -> List[str]:
        """Return short failing snippets across the first N files."""
        z = zipfile.ZipFile(io.BytesIO(zip_bytes))
        snippets: List[str] = []
        for i, name in enumerate(z.namelist()):
            if i >= max_files: break
            with z.open(name) as f:
                try:
                    text = f.read().decode("utf-8", errors="ignore")
                except Exception:
                    continue
            for line in text.splitlines():
                if FAIL_PAT.search(line):
                    snippets.append(line.strip())
                    if len(snippets) >= max_snippets: return snippets
        return snippets

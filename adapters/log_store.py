#log_store.py
from __future__ import annotations
import io, os, re, zipfile
from typing import List
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

API = os.getenv("GITHUB_API", "https://api.github.com")
TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "30"))
FAIL_PAT = re.compile(r"\b(FAIL|FAILED|ERROR|Traceback|AssertionError)\b", re.IGNORECASE)

def _session() -> requests.Session:
    s = requests.Session()
    retries = Retry(total=5, backoff_factor=0.3,
                    status_forcelist=[429,500,502,503,504],
                    allowed_methods=["GET"])
    s.mount("https://", HTTPAdapter(max_retries=retries))
    token = os.getenv("GITHUB_TOKEN")
    headers = {"Accept":"application/vnd.github+json"}
    if token: headers["Authorization"] = f"Bearer {token}"
    s.headers.update(headers); return s

class LogStore:
    def __init__(self, api_base: str = API):
        self.api = api_base
        self.s = _session()

    def fetch_run_logs_zip(self, repo: str, run_id: int) -> bytes:
        url = f"{self.api}/repos/{repo}/actions/runs/{run_id}/logs"
        r = self.s.get(url, timeout=TIMEOUT); r.raise_for_status()
        return r.content  # ZIP bytes

    def list_log_files(self, zip_bytes: bytes, limit: int=50) -> List[str]:
        z = zipfile.ZipFile(io.BytesIO(zip_bytes))
        return z.namelist()[:limit]

    def extract_failure_snippets(self, zip_bytes: bytes, max_files: int=10, max_snippets: int=20) -> List[str]:
        z = zipfile.ZipFile(io.BytesIO(zip_bytes))
        out: List[str] = []
        for i, name in enumerate(z.namelist()):
            if i >= max_files: break
            with z.open(name) as f:
                text = f.read().decode("utf-8", errors="ignore")
            for line in text.splitlines():
                if FAIL_PAT.search(line):
                    out.append(line.strip())
                    if len(out) >= max_snippets: return out
        return out

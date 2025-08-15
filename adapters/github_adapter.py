#github_adapter.py
from __future__ import annotations
import os
from typing import Any, Dict, List, Optional, Tuple
from pydantic import BaseModel
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

DEFAULT_API = os.getenv("GITHUB_API", "https://api.github.com")
TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "20"))

def _session() -> requests.Session:
    s = requests.Session()
    retries = Retry(total=5, backoff_factor=0.2,
                    status_forcelist=[429,500,502,503,504],
                    allowed_methods=["GET","POST","PATCH"])
    s.mount("https://", HTTPAdapter(max_retries=retries))
    token = os.getenv("GITHUB_TOKEN")
    headers = {"Accept": "application/vnd.github+json"}
    if token: headers["Authorization"] = f"Bearer {token}"
    s.headers.update(headers)
    return s

class Commit(BaseModel):
    sha: str
    message: str
    author_name: Optional[str]
    author_email: Optional[str]
    date: Optional[str]

class DiffFile(BaseModel):
    filename: str
    status: str
    additions: int
    deletions: int
    changes: int
    patch: Optional[str] = None

class PullRequest(BaseModel):
    number: int
    url: str
    html_url: str
    state: str
    title: str
    head: str
    base: str

class GitHub:
    def __init__(self, api_base: str = DEFAULT_API):
        self.api = api_base
        self.s = _session()

    def list_commits(self, repo: str, branch: Optional[str]=None,
                     per_page: int=30, max_pages: int=5) -> List[Commit]:
        url = f"{self.api}/repos/{repo}/commits"
        params = {"per_page":per_page}
        if branch: params["sha"] = branch
        out: List[Commit] = []
        for _ in range(max_pages):
            r = self.s.get(url, params=params, timeout=TIMEOUT); r.raise_for_status()
            for c in r.json():
                commit = c.get("commit", {})
                author = commit.get("author") or {}
                out.append(Commit(
                    sha=c.get("sha"), message=commit.get("message",""),
                    author_name=author.get("name"), author_email=author.get("email"),
                    date=author.get("date"),
                ))
            if "next" not in (r.links or {}): break
            url = r.links["next"]["url"]; params = None
        return out

    def compare(self, repo: str, base: str, head: str) -> Tuple[List[DiffFile], int, int]:
        url = f"{self.api}/repos/{repo}/compare/{base}...{head}"
        r = self.s.get(url, timeout=TIMEOUT); r.raise_for_status()
        js = r.json()
        files: List[DiffFile] = []
        for f in js.get("files", []):
            files.append(DiffFile(
                filename=f["filename"], status=f.get("status",""),
                additions=f.get("additions",0), deletions=f.get("deletions",0),
                changes=f.get("changes",0), patch=f.get("patch"),
            ))
        return files, js.get("ahead_by",0), js.get("behind_by",0)

    def open_pr(self, repo: str, head: str, base: str, title: str,
                body: str="", draft: bool=True) -> PullRequest:
        url = f"{self.api}/repos/{repo}/pulls"
        r = self.s.post(url, json={"head":head,"base":base,"title":title,
                                   "body":body,"draft":draft}, timeout=TIMEOUT)
        r.raise_for_status()
        js = r.json()
        return PullRequest(
            number=js["number"], url=js["url"], html_url=js["html_url"],
            state=js["state"], title=js["title"], head=js["head"]["ref"], base=js["base"]["ref"]
        )

    def rate_limit(self) -> Dict[str, Any]:
        r = self.s.get(f"{self.api}/rate_limit", timeout=TIMEOUT); r.raise_for_status()
        return r.json()

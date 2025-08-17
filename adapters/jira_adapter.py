# adapters/jira_adapter.py
from __future__ import annotations
import os
from typing import Any, Dict, Optional
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "20"))

def _session(email: str, api_token: str) -> requests.Session:
    s = requests.Session()
    retries = Retry(total=5, backoff_factor=0.2,
                    status_forcelist=[429,500,502,503,504],
                    allowed_methods=["GET","POST"])
    s.mount("https://", HTTPAdapter(max_retries=retries))
    s.auth = (email, api_token)
    s.headers.update({"Accept":"application/json","Content-Type":"application/json"})
    return s

# NEW: minimal helper to turn plain text into ADF
def _adf_from_text(text: str) -> Dict[str, Any]:
    text = text or ""
    # Preserve newlines as hardBreaks inside a single paragraph
    nodes = []
    parts = text.split("\n")
    for i, part in enumerate(parts):
        if part:
            nodes.append({"type": "text", "text": part})
        if i < len(parts) - 1:
            nodes.append({"type": "hardBreak"})
    if not nodes:
        nodes = [{"type": "text", "text": ""}]
    return {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": nodes}]}

class Jira:
    def __init__(self, base_url: Optional[str]=None,
                 email: Optional[str]=None,
                 api_token: Optional[str]=None):
        self.base = (base_url or os.getenv("JIRA_BASE_URL","")).rstrip("/")
        self.email = email or os.getenv("JIRA_EMAIL")
        # Accept either JIRA_API_TOKEN (preferred) or JIRA_TOKEN
        self.api_token = api_token or os.getenv("JIRA_API_TOKEN") or os.getenv("JIRA_TOKEN")
        if not (self.base and self.email and self.api_token):
            raise RuntimeError("JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN required")
        self.s = _session(self.email, self.api_token)

    def create_issue(self, project_key: str, summary: str,
                     description: str="", issue_type: str="Task") -> Dict[str, Any]:
        url = f"{self.base}/rest/api/3/issue"
        payload = {"fields":{
            "project":{"key":project_key},
            "summary":summary,
            # CHANGED: wrap description in ADF
            "description": _adf_from_text(description),
            "issuetype":{"name":issue_type},
        }}
        r = self.s.post(url, json=payload, timeout=TIMEOUT); r.raise_for_status()
        return r.json()

    def add_comment(self, issue_key: str, body: str) -> Dict[str, Any]:
        url = f"{self.base}/rest/api/3/issue/{issue_key}/comment"
        # CHANGED: comments are also ADF in v3
        r = self.s.post(url, json={"body": _adf_from_text(body)}, timeout=TIMEOUT); r.raise_for_status()
        return r.json()

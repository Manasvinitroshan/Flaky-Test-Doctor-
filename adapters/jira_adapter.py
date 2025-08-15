#jira_adapter.py
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

class Jira:
    def __init__(self, base_url: Optional[str]=None,
                 email: Optional[str]=None,
                 api_token: Optional[str]=None):
        self.base = (base_url or os.getenv("JIRA_BASE_URL","")).rstrip("/")
        self.email = email or os.getenv("JIRA_EMAIL")
        self.api_token = api_token or os.getenv("JIRA_API_TOKEN")
        if not (self.base and self.email and self.api_token):
            raise RuntimeError("JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN required")
        self.s = _session(self.email, self.api_token)

    def create_issue(self, project_key: str, summary: str,
                     description: str="", issue_type: str="Task") -> Dict[str, Any]:
        url = f"{self.base}/rest/api/3/issue"
        payload = {"fields":{
            "project":{"key":project_key},
            "summary":summary,
            "description":description,
            "issuetype":{"name":issue_type},
        }}
        r = self.s.post(url, json=payload, timeout=TIMEOUT); r.raise_for_status()
        return r.json()

    def add_comment(self, issue_key: str, body: str) -> Dict[str, Any]:
        url = f"{self.base}/rest/api/3/issue/{issue_key}/comment"
        r = self.s.post(url, json={"body":body}, timeout=TIMEOUT); r.raise_for_status()
        return r.json()

#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, re, subprocess, sys
from pathlib import Path
from typing import Dict, List, Tuple

_STATUS_RE = re.compile(r"(?P<nodeid>[^\s]+)\s+(PASSED|FAILED|ERROR|XPASSED|XFAILED|SKIPPED)\s*$", re.IGNORECASE)
_END_STATUS_RE = re.compile(r"\b(PASSED|FAILED|ERROR|XPASSED|XFAILED|SKIPPED)\b", re.IGNORECASE)

def _status_to_tok(s: str, include_skipped: bool) -> str | None:
    s = s.lower()
    if s in ("passed","xpassed"): return "pass"
    if s in ("failed","error","xfailed"): return "fail"
    if s == "skipped": return "pass" if include_skipped else None
    return None

def parse_plain(lines: List[str], include_skipped: bool) -> Dict[str,List[str]]:
    per: Dict[str,List[str]] = {}
    for ln in lines:
        m = _STATUS_RE.search(ln)
        if m:
            nodeid = m.group("nodeid")
            sm = _END_STATUS_RE.search(ln)
            if sm:
                tok = _status_to_tok(sm.group(1), include_skipped)
                if tok: per.setdefault(nodeid,[]).append(tok)
            continue
        sm = _END_STATUS_RE.search(ln)
        if sm and "::" not in ln:
            tok = _status_to_tok(sm.group(1), include_skipped)
            if tok: per.setdefault("__suite__",[]).append(tok)
    if not per:
        txt = "\n".join(lines)
        def c(p): 
            m = re.search(p, txt, re.IGNORECASE); return int(m.group(1)) if m else 0
        passed, failed, errored, skipped = c(r"(\d+)\s+passed"), c(r"(\d+)\s+failed"), c(r"(\d+)\s+error"), c(r"(\d+)\s+skipped")
        suite = ["pass"]*(passed + (skipped if include_skipped else 0)) + ["fail"]*(failed+errored)
        if suite: per["__suite__"] = suite
    return per

def run_pytest(py_args: str | None) -> List[str]:
    args = ["pytest"]
    if py_args: args += py_args.strip().split()
    proc = subprocess.run(args, capture_output=True, text=True)
    return ((proc.stdout or "") + "\n" + (proc.stderr or "")).splitlines()

def pick(per: Dict[str,List[str]], query: str) -> Tuple[str | None, List[str] | None]:
    q = query.lower()
    for k in per:
        if k.lower() == q: return k, per[k]
    cands = [(k,v) for k,v in per.items() if q in k.lower()]
    if cands:
        cands.sort(key=lambda kv: len(kv[1]), reverse=True)
        return cands[0]
    return None, None

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="*", help="pytest.out paths; omit with --run")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--pytest-args", default=None)
    ap.add_argument("--test", default=None)
    ap.add_argument("--by-test", action="store_true")
    ap.add_argument("--include-skipped", action="store_true")
    a = ap.parse_args()

    if a.run:
        lines = run_pytest(a.pytest_args)
        per = parse_plain(lines, include_skipped=a.include_skipped)
    else:
        if not a.paths:
            print("usage: pytest_to_history.py [pytest.out] [--run]", file=sys.stderr); sys.exit(2)
        lines: List[str] = []
        for p in a.paths:
            lines += Path(p).read_text(errors="ignore").splitlines()
        per = parse_plain(lines, include_skipped=a.include_skipped)

    if a.test:
        _, hist = pick(per, a.test)
        if hist is None:
            suite = per.get("__suite__", [])
            print(json.dumps(suite)); return
        print(json.dumps(hist)); return

    if a.by_test:
        print(json.dumps(per, indent=2, sort_keys=True)); return

    suite = per.get("__suite__")
    if suite: print(json.dumps(suite)); return
    agg: List[str] = []
    for k,v in per.items():
        if k == "__suite__": continue
        agg += v
    print(json.dumps(agg))

if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
pytest_to_history.py

Turn pytest results into a 'history' array that your MCP tool expects.

USAGE EXAMPLES
--------------
# Parse an existing plaintext pytest output file:
python cli/pytest_to_history.py path/to/pytest.out

# Parse an existing JUnit XML file (pytest --junitxml=report.xml):
python cli/pytest_to_history.py path/to/report.xml --junit

# Run pytest for me (quiet) and parse its output:
python cli/pytest_to_history.py --run

# Run pytest with extra args, output suite-level history:
python cli/pytest_to_history.py --run --pytest-args "-q -k login"

# Get history ONLY for a specific test (substring match on nodeid or name):
python cli/pytest_to_history.py path/to/pytest.out --test test_login

# Output per-test histories as a JSON object { "nodeid": ["pass","fail",...], ... }
python cli/pytest_to_history.py path/to/pytest.out --by-test

# Include skipped/xfailed as "pass" (default is to ignore them):
python cli/pytest_to_history.py path/to/pytest.out --include-skipped
"""

from __future__ import annotations
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Tuple


# ----------------------- Plain-text parsing -----------------------

_STATUS_RE = re.compile(
    r"""
    (?P<nodeid>[^\s]+)       # test node id up to whitespace
    \s+                      # spaces
    (?:
        PASSED|
        FAILED|
        ERROR|
        XPASSED|
        XFAILED|
        SKIPPED
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

_END_STATUS_RE = re.compile(r"\b(PASSED|FAILED|ERROR|XPASSED|XFAILED|SKIPPED)\b", re.IGNORECASE)

def _status_to_history_token(status: str, include_skipped: bool) -> str | None:
    s = status.lower()
    if s in ("passed", "xpassed"):
        return "pass"
    if s in ("failed", "error", "xfailed"):
        return "fail"
    if s == "skipped":
        return "pass" if include_skipped else None
    return None

def parse_pytest_plaintext(lines: List[str], include_skipped: bool) -> Dict[str, List[str]]:
    """
    Return per-test history mapping: { nodeid: ["pass"|"fail", ...] }
    """
    per_test: Dict[str, List[str]] = {}

    for ln in lines:
        # Prefer lines that look like: tests/test_example.py::test_foo PASSED
        m = _STATUS_RE.search(ln)
        if m:
            nodeid = m.group("nodeid")
            status_match = _END_STATUS_RE.search(ln)
            if status_match:
                tok = _status_to_history_token(status_match.group(1), include_skipped)
                if tok:
                    per_test.setdefault(nodeid, []).append(tok)
            continue

        # Fallback: detect bare status lines (no nodeid). Aggregate as suite-level pseudo-node.
        status_match = _END_STATUS_RE.search(ln)
        if status_match and "::" not in ln:
            tok = _status_to_history_token(status_match.group(1), include_skipped)
            if tok:
                per_test.setdefault("__suite__", []).append(tok)

    # If nothing matched but we have a summary line, parse that:
    if not per_test:
        txt = "\n".join(lines)
        passed = _extract_count(txt, r"(\d+)\s+passed")
        failed = _extract_count(txt, r"(\d+)\s+failed")
        errored = _extract_count(txt, r"(\d+)\s+error")
        xpassed = _extract_count(txt, r"(\d+)\s+xpassed")
        xfailed = _extract_count(txt, r"(\d+)\s+xfailed")
        skipped = _extract_count(txt, r"(\d+)\s+skipped")

        suite: List[str] = []
        suite += ["pass"] * (passed + xpassed + (skipped if include_skipped else 0))
        suite += ["fail"] * (failed + errored + xfailed)
        if suite:
            per_test["__suite__"] = suite

    return per_test


def _extract_count(text: str, pattern: str) -> int:
    m = re.search(pattern, text, re.IGNORECASE)
    return int(m.group(1)) if m else 0


# ----------------------- JUnit XML parsing -----------------------

def parse_junit_xml(paths: List[Path], include_skipped: bool) -> Dict[str, List[str]]:
    """
    Parse one or more JUnit XML files and return { nodeid: ["pass"/"fail", ...] }.
    """
    per_test: Dict[str, List[str]] = {}

    for p in paths:
        tree = ET.parse(p)
        root = tree.getroot()
        # xUnit schema: <testsuite> contains <testcase ...> with children like <failure/>, <error/>, <skipped/>
        for tc in root.iter("testcase"):
            classname = tc.attrib.get("classname", "")
            name = tc.attrib.get("name", "")
            nodeid = f"{classname}::{name}" if classname else name

            status = "passed"
            if tc.find("failure") is not None or tc.find("error") is not None:
                status = "failed"
            elif tc.find("skipped") is not None:
                status = "skipped"

            tok = _status_to_history_token(status, include_skipped)
            if tok:
                per_test.setdefault(nodeid, []).append(tok)

    # If still empty, make a best-effort suite summary from testsuite attrs
    if not per_test:
        for p in paths:
            tree = ET.parse(p)
            for ts in tree.getroot().iter("testsuite"):
                passed = int(ts.attrib.get("tests", "0")) - int(ts.attrib.get("failures", "0")) - int(ts.attrib.get("errors", "0")) - int(ts.attrib.get("skipped", "0"))
                failed = int(ts.attrib.get("failures", "0")) + int(ts.attrib.get("errors", "0"))
                skipped = int(ts.attrib.get("skipped", "0"))
                suite: List[str] = []
                suite += ["pass"] * (passed + (skipped if include_skipped else 0))
                suite += ["fail"] * failed
                if suite:
                    per_test["__suite__"] = per_test.get("__suite__", []) + suite
    return per_test


# ----------------------- Running pytest -----------------------

def run_pytest_and_capture(pytest_args: str | None) -> List[str]:
    args = ["pytest"]
    if pytest_args:
        # naive split is fine for typical usages; for complex cases, advise quoting in shell
        args += pytest_args.strip().split()
    proc = subprocess.run(args, capture_output=True, text=True)
    # We use stdout for per-test lines; keep stderr around but not needed for parsing
    combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
    return combined.splitlines()


# ----------------------- CLI -----------------------

def main():
    ap = argparse.ArgumentParser(description="Convert pytest results into a history array for MCP tools.")
    ap.add_argument("paths", nargs="*", help="pytest.out or JUnit XML file(s). If omitted, use --run to execute pytest.")
    ap.add_argument("--run", action="store_true", help="Run pytest and parse its output instead of reading files.")
    ap.add_argument("--pytest-args", default=None, help="Extra args for pytest (e.g., '-q -k login').")
    ap.add_argument("--junit", action="store_true", help="Treat input paths as JUnit XML files.")
    ap.add_argument("--test", default=None, help="Filter to a specific test (substring of nodeid/name).")
    ap.add_argument("--by-test", action="store_true", help="Output per-test histories as a JSON object.")
    ap.add_argument("--include-skipped", action="store_true", help='Count "skipped"/x* as pass (default: ignore).')
    args = ap.parse_args()

    per_test: Dict[str, List[str]] = {}

    if args.run:
        lines = run_pytest_and_capture(args.pytest_args)
        per_test = parse_pytest_plaintext(lines, include_skipped=args.include_skipped)
    else:
        if not args.paths:
            print("error: Provide paths to parse or use --run", file=sys.stderr)
            sys.exit(2)
        paths = [Path(p) for p in args.paths]
        if args.junit or any(str(p).lower().endswith(".xml") for p in paths):
            per_test = parse_junit_xml(paths, include_skipped=args.include_skipped)
        else:
            # Plain text mode: concatenate all files
            lines: List[str] = []
            for p in paths:
                lines += p.read_text(errors="ignore").splitlines()
            per_test = parse_pytest_plaintext(lines, include_skipped=args.include_skipped)

    # If user requested a specific test, find the best match
    if args.test:
        target, hist = _pick_best_match(per_test, args.test)
        if hist is None:
            # Fall back to suite if present
            suite = per_test.get("__suite__")
            if suite:
                print(json.dumps(suite))
                return
            print("[]")
            return
        # Output just the history array for that test
        print(json.dumps(hist))
        return

    # If user wants per-test map, return all
    if args.by_test:
        print(json.dumps(per_test, indent=2, sort_keys=True))
        return

    # Default: suite-level history only
    suite = per_test.get("__suite__")
    if suite is not None:
        print(json.dumps(suite))
        return

    # If no suite, aggregate across all tests into one array (order not guaranteed)
    agg: List[str] = []
    for nodeid, hist in per_test.items():
        if nodeid == "__suite__":
            continue
        agg += hist
    print(json.dumps(agg))

def _pick_best_match(per_test: Dict[str, List[str]], query: str) -> Tuple[str | None, List[str] | None]:
    query_low = query.lower()
    # exact match first
    for k in per_test:
        if k.lower() == query_low:
            return k, per_test[k]
    # substring fallback
    candidates = [(k, v) for k, v in per_test.items() if query_low in k.lower()]
    if candidates:
        # pick the longest history (most signal)
        candidates.sort(key=lambda kv: len(kv[1]), reverse=True)
        return candidates[0]
    return None, None


if __name__ == "__main__":
    main()

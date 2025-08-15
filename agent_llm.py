#!/usr/bin/env python3
"""
LLM-driven agent that plans with tool-calling against the MCP stdio server.

Features:
- Loads .env explicitly from repo root.
- CLI args: --test-name, --history (path or JSON), --logs (path), --diff (path).
- If PROMPT is unset, builds a contextual prompt from inputs.
- Graceful fallback to offline deterministic plan when OpenAI key/quota fails.
"""

from __future__ import annotations
import argparse
import json
import os
import subprocess
import sys
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

# ──────────────────────────────────────────────────────────────────────────────
# Env loading
# ──────────────────────────────────────────────────────────────────────────────
REPO_ROOT = os.getcwd()
load_dotenv(dotenv_path=os.path.join(REPO_ROOT, ".env"))

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL   = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
PYTHON_BIN     = os.getenv("PYTHON", sys.executable)
SERVER_CMD     = [PYTHON_BIN, "mcp_server.py"]

# ──────────────────────────────────────────────────────────────────────────────
# Utilities
# ──────────────────────────────────────────────────────────────────────────────
def read_text(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except FileNotFoundError:
        print(f"[warn] File not found: {path}", file=sys.stderr)
        return None

def parse_history_arg(history_arg: Optional[str]) -> Optional[List[str]]:
    """
    Accepts:
      - a path to a JSON file (e.g., history.json)
      - a JSON array literal (e.g., '["pass","fail","pass"]')
      - None
    Returns: list[str] or None
    """
    if not history_arg:
        return None
    # If it's a path, try to read it
    if os.path.exists(history_arg):
        try:
            with open(history_arg, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[warn] Could not parse JSON in {history_arg}: {e}", file=sys.stderr)
            return None
    # Else, try to treat as JSON literal
    try:
        return json.loads(history_arg)
    except Exception:
        print(f"[warn] --history not valid JSON or file path: {history_arg}", file=sys.stderr)
        return None

def build_prompt(test_name: str, history: Optional[List[str]], logs: Optional[str], diff: Optional[str]) -> str:
    # If user provided PROMPT, prefer that
    if os.getenv("PROMPT"):
        return os.getenv("PROMPT")  # type: ignore[return-value]

    # Otherwise, craft a contextual prompt
    lines = []
    lines.append(f"Test under investigation: {test_name or 'suite'}")
    if history is not None:
        lines.append(f"History (most recent first?): {history}")
    else:
        lines.append("History: (not provided)")

    if logs:
        snippet = logs.strip()
        if len(snippet) > 2000:
            snippet = snippet[:2000] + "…"
        lines.append("Recent failing log snippet:\n" + snippet)

    if diff:
        d = diff.strip()
        if len(d) > 3000:
            d = d[:3000] + "…"
        lines.append("Recent code diff context:\n" + d)

    lines.append(
        "Task: Determine whether the test is flaky, classify the type (flaky vs regression vs infra), "
        "and propose concrete, code-level fixes. If helpful, call tools "
        "`is_flaky(test_name, history)` and `suggest_fix(test_name, history)`."
    )
    lines.append(
        "Return a concise summary with bullet-point suggestions; include examples (e.g., replace time.sleep with condition-based waits)."
    )
    return "\n\n".join(lines)

def jsonrpc_call(proc: subprocess.Popen, method: str, params: Optional[Dict[str, Any]] = None, id_: int = 1) -> Dict[str, Any]:
    req = {"jsonrpc": "2.0", "id": id_, "method": method}
    if params is not None:
        req["params"] = params
    proc.stdin.write(json.dumps(req) + "\n")  # type: ignore[arg-type]
    proc.stdin.flush()  # type: ignore[union-attr]
    line = proc.stdout.readline()  # type: ignore[union-attr]
    if not line:
        return {"error": {"message": "no response"}}
    try:
        return json.loads(line)
    except Exception as e:
        return {"error": {"message": f"invalid json: {e}", "raw": line}}

def to_openai_tools(mcp_tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    # Map MCP tool schemas to OpenAI "function" tools
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", f"MCP tool {t['name']}"),
                "parameters": t["input"],  # already JSON Schema
            },
        }
        for t in mcp_tools
    ]

def run_offline_plan(test_name: str, history: Optional[List[str]]):
    """No OpenAI or error: still exercise MCP tools for a deterministic demo."""
    print("[offline] Using deterministic plan (no OpenAI).\n")
    hist = history or ["pass", "fail", "pass", "fail"]
    with subprocess.Popen(SERVER_CMD, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, bufsize=1) as proc:
        print("→ mcp.list_tools")
        print(json.dumps(jsonrpc_call(proc, "mcp.list_tools", id_=1), indent=2))

        print(f"\n→ is_flaky({test_name})")
        print(json.dumps(jsonrpc_call(proc, "is_flaky",
            {"test_name": test_name, "history": hist}, id_=2), indent=2))

        print(f"\n→ suggest_fix({test_name})")
        print(json.dumps(jsonrpc_call(proc, "suggest_fix",
            {"test_name": test_name, "history": hist}, id_=3), indent=2))

# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="LLM agent that orchestrates MCP tools.")
    ap.add_argument("--test-name", default=os.getenv("TEST_NAME", "login_test"))
    ap.add_argument("--history", help="Path to history.json or a JSON array literal.")
    ap.add_argument("--logs", help="Path to a failing logs snippet (plain text).")
    ap.add_argument("--diff", help="Path to a code diff (e.g., git diff output).")
    args = ap.parse_args()

    test_name = args.test_name
    history = parse_history_arg(args.history)
    logs = read_text(args.logs)
    diff = read_text(args.diff)

    prompt = build_prompt(test_name, history, logs, diff)

    # If no OpenAI key, do deterministic offline path
    if not OPENAI_API_KEY:
        print("[warn] OPENAI_API_KEY missing; falling back to offline mode.")
        return run_offline_plan(test_name, history)

    # Try the LLM path; on 401/429/etc., gracefully fall back
    try:
        import openai
        openai.api_key = OPENAI_API_KEY

        with subprocess.Popen(SERVER_CMD, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, bufsize=1) as proc:

            tools_manifest = jsonrpc_call(proc, "mcp.list_tools", id_=1)
            if "error" in tools_manifest:
                raise RuntimeError(f"MCP error: {tools_manifest['error']}")
            mcp_tools = tools_manifest["result"]["tools"]
            oai_tools = to_openai_tools(mcp_tools)

            messages = [{"role": "user", "content": prompt}]

            first = openai.chat.completions.create(
                model=OPENAI_MODEL,
                messages=messages,
                tools=oai_tools,
                tool_choice="auto",
            )
            msg = first.choices[0].message
            messages.append(msg)

            # If the model decides to call tools, loop through them
            if msg.tool_calls:
                for tc in msg.tool_calls:
                    name = tc.function.name
                    args_json = json.loads(tc.function.arguments or "{}")
                    # Supply defaults if not provided
                    args_json.setdefault("test_name", test_name)
                    if history is not None:
                        args_json.setdefault("history", history)

                    out = jsonrpc_call(proc, name, args_json, id_=len(messages) + 1)
                    if "error" in out:
                        tool_content = json.dumps({"error": out["error"]})
                    else:
                        tool_content = json.dumps(out["result"])
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "name": name,
                        "content": tool_content,
                    })

            # Ask the model to summarize with tool outputs
            final = openai.chat.completions.create(
                model=OPENAI_MODEL,
                messages=messages
            )
            print(final.choices[0].message.content)

    except Exception as e:
        print(f"[warn] LLM path failed ({type(e).__name__}): {e}\n"
              f"Falling back to offline mode.", file=sys.stderr)
        return run_offline_plan(test_name, history)

# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    main()

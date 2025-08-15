#!/usr/bin/env python3
import json, subprocess, sys

SERVER_CMD = [sys.executable, "mcp_server.py"]

def jsonrpc_call(proc, method, params=None, id_=1):
    req = {"jsonrpc": "2.0", "id": id_, "method": method}
    if params is not None:
        req["params"] = params
    proc.stdin.write(json.dumps(req) + "\n"); proc.stdin.flush()
    return json.loads(proc.stdout.readline())

def main():
    with subprocess.Popen(
        SERVER_CMD, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, bufsize=1
    ) as proc:
        print("→ mcp.list_tools")
        tools = jsonrpc_call(proc, "mcp.list_tools", id_=1)
        print(json.dumps(tools, indent=2))

        hist = ["pass","fail","pass","fail"]
        print("\n→ is_flaky")
        print(json.dumps(jsonrpc_call(proc, "is_flaky",
              {"test_name":"login_test","history":hist}, id_=2), indent=2))

        print("\n→ suggest_fix")
        print(json.dumps(jsonrpc_call(proc, "suggest_fix",
              {"test_name":"login_test","history":hist}, id_=3), indent=2))

if __name__ == "__main__":
    main()

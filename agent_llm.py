#!/usr/bin/env python3
import json, os, subprocess, sys

# --- Config ---
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL   = os.getenv("OPENAI_MODEL", "gpt-4o-mini")  # choose your model
SERVER_CMD     = [sys.executable, "mcp_server.py"]

def jsonrpc_call(proc, method, params=None, id_=1):
    req = {"jsonrpc":"2.0","id":id_,"method":method}
    if params is not None:
        req["params"] = params
    proc.stdin.write(json.dumps(req) + "\n"); proc.stdin.flush()
    return json.loads(proc.stdout.readline())

def to_openai_tools(mcp_tools):
    tools = []
    for t in mcp_tools:
        tools.append({
            "type":"function",
            "function":{
                "name": t["name"],
                "description": t.get("description", f"MCP tool {t['name']}"),
                "parameters": t["input"]
            }
        })
    return tools

def main():
    if not OPENAI_API_KEY:
        print("Set OPENAI_API_KEY to use agent_llm.py", file=sys.stderr)
        sys.exit(1)

    import openai
    openai.api_key = OPENAI_API_KEY

    prompt = os.getenv("PROMPT", "We saw test 'login_test' with history pass, fail, pass, fail. Is it flaky? Suggest a fix.")

    with subprocess.Popen(SERVER_CMD, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, bufsize=1) as proc:
        tools_manifest = jsonrpc_call(proc, "mcp.list_tools", id_=1)
        mcp_tools = tools_manifest["result"]["tools"]
        tools = to_openai_tools(mcp_tools)

        messages = [{"role":"user","content":prompt}]
        # first call: let LLM decide tools to call
        resp = openai.chat.completions.create(
            model=OPENAI_MODEL, messages=messages, tools=tools, tool_choice="auto"
        )
        msg = resp.choices[0].message
        messages.append(msg)

        # execute MCP tool calls
        if msg.tool_calls:
            for tc in msg.tool_calls:
                name = tc.function.name
                args = json.loads(tc.function.arguments or "{}")
                out = jsonrpc_call(proc, name, params=args, id_=len(messages)+1)
                messages.append({
                    "role":"tool",
                    "tool_call_id": tc.id,
                    "name": name,
                    "content": json.dumps(out["result"])
                })

        # final reasoning call
        final = openai.chat.completions.create(model=OPENAI_MODEL, messages=messages)
        print(final.choices[0].message.content)

if __name__ == "__main__":
    main()

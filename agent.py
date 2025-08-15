# agent.py
import os, json, asyncio, aiohttp
from typing import Any, Dict, List

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")  # pick your model
BASE_URL = os.getenv("MCP_URL", "http://127.0.0.1:9000")
API_KEY = os.getenv("OPENAI_API_KEY")

async def fetch_mcp_tools(session: aiohttp.ClientSession) -> Dict[str, Any]:
    async with session.get(f"{BASE_URL}/.well-known/mcp-tools") as r:
        r.raise_for_status()
        return await r.json()

def to_openai_tools(mcp_manifest: Dict[str, Any]) -> List[Dict[str, Any]]:
    tools = []
    for t in mcp_manifest["tools"]:
        tools.append({
            "type": "function",
            "function": {
                "name": t["name"],
                "description": f"MCP tool: {t['name']}",
                "parameters": t["input"]  # JSON Schema
            }
        })
    return tools

async def call_server_tool(session, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    # Route tool names -> HTTP endpoints (MVP mapping)
    url = f"{BASE_URL}/{name}"
    async with session.post(url, json=args) as r:
        r.raise_for_status()
        return await r.json()

async def run_once(user_prompt: str):
    import openai
    openai.api_key = API_KEY

    async with aiohttp.ClientSession() as session:
        manifest = await fetch_mcp_tools(session)
        tools = to_openai_tools(manifest)

        messages = [{"role":"user","content":user_prompt}]
        # 1st call: let model decide which tool(s) to call
        resp = openai.chat.completions.create(
            model=OPENAI_MODEL,
            messages=messages,
            tools=tools,
            tool_choice="auto"
        )
        msg = resp.choices[0].message
        messages.append(msg)

        # Execute any tool calls
        tool_results = []
        if msg.tool_calls:
            for tc in msg.tool_calls:
                name = tc.function.name
                args = json.loads(tc.function.arguments or "{}")
                out = await call_server_tool(session, name, args)
                tool_results.append({"tool_call_id": tc.id, "name": name, "output": out})
                messages.append({
                    "role":"tool",
                    "tool_call_id": tc.id,
                    "name": name,
                    "content": json.dumps(out)
                })

        # Final reasoning turn
        final = openai.chat.completions.create(
            model=OPENAI_MODEL,
            messages=messages
        )
        print(final.choices[0].message.content)

if __name__ == "__main__":
    # Example prompt; pass your own via CLI or env if you prefer
    prompt = os.getenv("PROMPT", "We saw test 'login_test' with history pass, fail, pass, fail. Is it flaky? Suggest a fix.")
    asyncio.run(run_once(prompt))

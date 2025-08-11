from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from typing import List

# Third-party MCP helper
from fastapi_mcp import FastApiMCP

# ─── FastAPI app ───────────────────────────────────────────────
app = FastAPI()

# ─── MCP integration ───────────────────────────────────────────
mcp = FastApiMCP(
    app,
    name="FlakyTestDoctor",
    description="Detects flaky tests based on historical pass/fail outcomes",
)
mcp.mount_http()                     # HTTP transport (recommended)

# ─── Templates & static assets ─────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# If you downloaded Bootstrap locally, keep this; otherwise comment it out.
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

# ─── Data models ───────────────────────────────────────────────
class FlakyRequest(BaseModel):
    test_name: str
    history: List[str]

class FlakyResponse(BaseModel):
    flaky: bool
    failures: int

# ─── API endpoint (becomes MCP tool) ───────────────────────────
@app.post("/is_flaky", response_model=FlakyResponse, operation_id="is_flaky")
async def is_flaky_endpoint(request: FlakyRequest):
    failures = sum(1 for s in request.history if s.lower() == "fail")
    return FlakyResponse(flaky=failures > 1, failures=failures)

# ─── Browser landing page ──────────────────────────────────────
@app.get("/", include_in_schema=False)
def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

# ─── Run via “python mcp_server.py” (dev only) ─────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=9000)

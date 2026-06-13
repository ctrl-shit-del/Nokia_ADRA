import json
import asyncio
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import installer_agent
import inventory_agent
import packages_agent
import requirements_agent
from adra_common import list_requirement_profiles, set_token_listener


app = FastAPI(title="ADRA API", version="2.0")
WEB_DIR = Path(__file__).parent / "web"
UPLOAD_DIR = Path(__file__).parent / "uploads"
TOKEN_QUEUES: set[asyncio.Queue] = set()


class AgentContext(BaseModel):
    context: dict[str, Any] = {}


@app.on_event("startup")
async def configure_token_events() -> None:
    loop = asyncio.get_running_loop()

    def publish(tokens: dict[str, int]) -> None:
        for queue in list(TOKEN_QUEUES):
            loop.call_soon_threadsafe(queue.put_nowait, tokens)

    set_token_listener(publish)


def result_with_tokens(result: dict[str, Any]) -> dict[str, Any]:
    result.setdefault("tokens_used", {"prompt": 0, "completion": 0, "total": 0})
    return result


@app.get("/api/profiles")
def profiles() -> dict[str, list[str]]:
    return {"profiles": list_requirement_profiles()}


@app.post("/api/requirements/run")
def run_requirements(payload: AgentContext) -> dict[str, Any]:
    return result_with_tokens(requirements_agent.run(payload.context))


@app.post("/api/requirements/upload")
async def upload_requirements(
    file: UploadFile = File(...),
    save_profile: bool = Form(False),
    version: str | None = Form(None),
) -> dict[str, Any]:
    UPLOAD_DIR.mkdir(exist_ok=True)
    safe_name = Path(file.filename or "upload").name
    path = UPLOAD_DIR / safe_name
    path.write_bytes(await file.read())
    context = {
        "mode": "upload",
        "document_path": str(path),
        "save_profile": save_profile,
        "version": version,
    }
    return result_with_tokens(requirements_agent.run(context))


@app.post("/api/inventory/run")
def run_inventory(payload: AgentContext) -> dict[str, Any]:
    return result_with_tokens(inventory_agent.run(payload.context))


@app.post("/api/packages/run")
def run_packages(payload: AgentContext) -> dict[str, Any]:
    return result_with_tokens(packages_agent.run(payload.context))


@app.post("/api/installer/run")
def run_installer(payload: AgentContext) -> dict[str, Any]:
    return result_with_tokens(installer_agent.run(payload.context))


@app.get("/api/state/{filename}")
def read_state(filename: str) -> dict[str, Any]:
    allowed = {"requirements.json", "inventory.json", "session_report.md"}
    if filename not in allowed:
        return {"status": "error", "error": "unsupported state file"}
    path = Path(filename)
    if not path.exists():
        return {"status": "missing", "content": None}
    if filename.endswith(".json"):
        return {"status": "ok", "content": json.loads(path.read_text(encoding="utf-8"))}
    return {"status": "ok", "content": path.read_text(encoding="utf-8")}


@app.get("/api/events")
async def events() -> StreamingResponse:
    queue: asyncio.Queue = asyncio.Queue()
    TOKEN_QUEUES.add(queue)

    async def stream():
        try:
            yield "event: ready\ndata: {}\n\n"
            while True:
                tokens = await queue.get()
                yield f"event: tokens\ndata: {json.dumps(tokens)}\n\n"
        finally:
            TOKEN_QUEUES.discard(queue)

    return StreamingResponse(stream(), media_type="text/event-stream")


if WEB_DIR.exists():
    app.mount("/web", StaticFiles(directory=WEB_DIR), name="web")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")

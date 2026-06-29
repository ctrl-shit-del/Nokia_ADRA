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
from adra_common import list_requirement_profiles, set_token_listener, LLAMACPP_URL
import requests

import sys
import paramiko


app = FastAPI(title="ADRA API", version="2.0")
WEB_DIR = Path(__file__).parent / "web"
UPLOAD_DIR = Path(__file__).parent / "uploads"
TOKEN_QUEUES: set[asyncio.Queue] = set()


class AgentContext(BaseModel):
    context: dict[str, Any] = {}


@app.on_event("startup")
async def configure_token_events() -> None:
    loop = asyncio.get_running_loop()

    def publish_event(data: dict[str, Any]) -> None:
        for queue in list(TOKEN_QUEUES):
            loop.call_soon_threadsafe(queue.put_nowait, data)

    set_token_listener(publish_event)
    
    class SSEStreamWrapper:
        def __init__(self, original_stdout):
            self.original_stdout = original_stdout

        def write(self, data):
            self.original_stdout.write(data)
            if data:
                try:
                    # Safely push to asyncio queues from any background thread
                    for queue in list(TOKEN_QUEUES):
                        loop.call_soon_threadsafe(queue.put_nowait, {"log": data})
                except Exception:
                    pass

        def flush(self):
            self.original_stdout.flush()

    sys.stdout = SSEStreamWrapper(sys.stdout)


def result_with_tokens(result: dict[str, Any]) -> dict[str, Any]:
    result.setdefault("tokens_used", {"prompt": 0, "completion": 0, "total": 0})
    return result


@app.get("/api/profiles")
def profiles() -> dict[str, list[str]]:
    return {"profiles": list_requirement_profiles()}


@app.get("/api/models")
def get_models() -> dict[str, list[str]]:
    base_url = LLAMACPP_URL.split("/chat/completions")[0]
    models_url = f"{base_url}/models"
    try:
        resp = requests.get(models_url, timeout=2)
        resp.raise_for_status()
        data = resp.json()
        models = data.get("data", [])
        return {"models": [m["id"] for m in models]}
    except Exception as e:
        print(f"Failed to fetch models: {e}")
        return {"models": ["mock-model"]}


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
        "mode": "upload" if not save_profile else "custom",
        "document_path": str(path),
        "save_profile": save_profile,
        "version": version,
    }
    try:
        return result_with_tokens(requirements_agent.run(context))
    finally:
        if path.exists():
            path.unlink()


import concurrent.futures

@app.post("/api/inventory/run")
def run_inventory(payload: AgentContext) -> dict[str, Any]:
    context = payload.context
    hosts = context.get("hosts")
    
    if not hosts:
        # Fallback for single host execution
        res = result_with_tokens(inventory_agent.run(context))
        if res.get("status") == "ok":
            host = context.get("host", "127.0.0.1")
            try:
                Path("inventory.json").write_text(Path(f"inventory_{host}.json").read_text(encoding="utf-8"), encoding="utf-8")
            except Exception:
                pass
        return res
        
    results = []
    total_tokens = {"prompt": 0, "completion": 0, "total": 0}
    
    def run_single(host_ctx):
        # We merge the base context with the specific host context
        merged_ctx = {**context, **host_ctx}
        return inventory_agent.run(merged_ctx)

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(run_single, h): h for h in hosts}
        for future in concurrent.futures.as_completed(futures):
            try:
                res = future.result()
                h = futures[future]
                res["host"] = h.get("host")
                results.append(res)
                
                # Copy to inventory.json to keep other tabs functioning with latest state
                if res.get("status") == "ok" and res.get("host"):
                    host_ip = res["host"]
                    try:
                        Path("inventory.json").write_text(Path(f"inventory_{host_ip}.json").read_text(encoding="utf-8"), encoding="utf-8")
                    except Exception:
                        pass
                
                t = res.get("tokens_used", {})
                total_tokens["prompt"] += t.get("prompt", 0)
                total_tokens["completion"] += t.get("completion", 0)
                total_tokens["total"] += t.get("total", 0)
            except Exception as e:
                h = futures[future]
                results.append({"status": "error", "host": h.get("host"), "error": str(e)})

    return {"status": "ok", "results": results, "tokens_used": total_tokens}


@app.post("/api/packages/run")
def run_packages(payload: AgentContext) -> dict[str, Any]:
    return result_with_tokens(packages_agent.run(payload.context))


@app.post("/api/installer/run")
def run_installer(payload: AgentContext) -> dict[str, Any]:
    return result_with_tokens(installer_agent.run(payload.context))


@app.get("/api/state/{filename}")
def read_state(filename: str) -> dict[str, Any]:
    allowed = {"requirements.json", "inventory.json", "session_report.md"}
    is_inventory_host = filename.startswith("inventory_") and filename.endswith(".json")
    if filename not in allowed and not is_inventory_host:
        return {"status": "error", "error": "unsupported state file"}
    path = Path(filename)
    if not path.exists():
        return {"status": "missing", "content": None}
    if filename.endswith(".json"):
        return {"status": "ok", "content": json.loads(path.read_text(encoding="utf-8"))}
    return {"status": "ok", "content": path.read_text(encoding="utf-8")}


@app.post("/api/terminal/run")
def run_terminal_command(payload: AgentContext) -> dict[str, Any]:
    context = payload.context
    host = context.get("host", "127.0.0.1")
    port = int(context.get("port", 22))
    username = context.get("username", "mystic")
    password = context.get("password", "")
    command = context.get("command", "")
    
    if not command:
        return {"status": "error", "error": "No command provided"}

    print(f"\\n> {command}")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        if password:
            client.connect(hostname=host, port=port, username=username, password=password, timeout=10)
        else:
            client.connect(hostname=host, port=port, username=username, timeout=10)
            
        stdin, stdout, stderr = client.exec_command(command)
        exit_code = stdout.channel.recv_exit_status()
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        combined = "\\n".join(filter(None, [out, err]))
        
        if combined:
            print(combined)
            
        return {"status": "ok", "output": combined, "exit_code": exit_code}
    except Exception as e:
        error_msg = str(e)
        print(f"[SSH Error] {error_msg}")
        return {"status": "error", "error": error_msg}
    finally:
        client.close()


@app.get("/api/events")
async def events() -> StreamingResponse:
    queue: asyncio.Queue = asyncio.Queue()
    TOKEN_QUEUES.add(queue)

    async def stream():
        try:
            yield "event: ready\\ndata: {}\\n\\n"
            while True:
                data = await queue.get()
                if "log" in data:
                    yield f"event: log\\ndata: {json.dumps(data)}\\n\\n"
                else:
                    yield f"event: tokens\\ndata: {json.dumps(data)}\\n\\n"
        finally:
            TOKEN_QUEUES.discard(queue)

    return StreamingResponse(stream(), media_type="text/event-stream")


if WEB_DIR.exists():
    app.mount("/web", StaticFiles(directory=WEB_DIR), name="web")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")

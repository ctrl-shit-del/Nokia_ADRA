import os
import sys
import subprocess
import threading
import socket
import time
import json
import logging
import re
import shutil
from pathlib import Path
from typing import Any, Generator
import requests
import psutil

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

# Configuration
LLAMA_CPP_DIR = Path(os.getenv("LLAMA_CPP_DIR", os.path.expanduser("~/llama.cpp")))
MODELS_DIR = Path(os.getenv("MODELS_DIR", os.path.expanduser("~/models")))
BINARY_PATH = LLAMA_CPP_DIR / "build" / "bin" / "llama-server"
LOGS_DIR = Path(__file__).parent / "logs"

# Ensure directories exist
MODELS_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)

# Logging configuration
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("llamacpp_manager")

app = FastAPI(title="llama.cpp Manager API", version="1.0")

# Global state
class State:
    build_process: subprocess.Popen | None = None
    build_logs: list[str] = []
    build_status: str = "idle"  # idle, building, success, failed
    build_cuda: bool = False
    
    # Downloads state: repo_id -> download details
    active_downloads: dict[str, dict[str, Any]] = {}
    # SSE progress listeners queues
    download_queues: set[Any] = set()
    build_queues: set[Any] = set()
    
    # Process handles for servers started *this* session
    # name -> dict of details
    servers: dict[str, dict[str, Any]] = {}

state = State()

# Helper utilities
def check_cuda_available() -> bool:
    """Check if nvidia-smi is present on the host."""
    return subprocess.run(["which", "nvidia-smi"], capture_output=True).returncode == 0

def is_port_in_use(port: int) -> bool:
    """Check if a port is open/in-use."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0

def get_lan_ips() -> list[str]:
    """Retrieve all LAN IP addresses of the host machine."""
    ips = []
    # Try connecting to external address to discover primary interface IP
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        primary_ip = s.getsockname()[0]
        ips.append(primary_ip)
        s.close()
    except Exception:
        pass
    
    # Fallback/additional check for all interfaces
    try:
        for interface, addrs in psutil.net_if_addrs().items():
            for addr in addrs:
                if addr.family == socket.AF_INET:
                    ip = addr.address
                    if not ip.startswith("127.") and ip not in ips:
                        ips.append(ip)
    except Exception:
        pass
    
    if not ips:
        ips.append("127.0.0.1")
    return ips

# Background Tasks
def run_build_task(cuda: bool):
    state.build_status = "building"
    state.build_logs = []
    state.build_cuda = cuda
    
    def log_and_broadcast(message: str):
        msg = message.strip()
        if msg:
            state.build_logs.append(msg)
            # Limit stored logs size to last 2000 lines
            if len(state.build_logs) > 2000:
                state.build_logs.pop(0)
            # Push to queues
            for q in list(state.build_queues):
                q.put_nowait(msg)
    
    try:
        # 1. Clone repo if needed
        if not LLAMA_CPP_DIR.exists():
            log_and_broadcast("Cloning llama.cpp repository...")
            cmd = ["git", "clone", "https://github.com/ggerganov/llama.cpp.git", str(LLAMA_CPP_DIR)]
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            for line in proc.stdout:
                log_and_broadcast(line)
            proc.wait()
            if proc.returncode != 0:
                state.build_status = "failed"
                log_and_broadcast("Git clone failed!")
                return
        
        # 2. Configure build
        log_and_broadcast("Configuring build directory using cmake...")
        build_dir = LLAMA_CPP_DIR / "build"
        build_dir.mkdir(parents=True, exist_ok=True)
        
        cmake_cmd = ["cmake", "..", "-DCMAKE_BUILD_TYPE=Release"]
        if cuda:
            cmake_cmd.append("-DGGML_CUDA=ON")
        else:
            cmake_cmd.append("-DGGML_CUDA=OFF")
            
        proc = subprocess.Popen(cmake_cmd, cwd=str(build_dir), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in proc.stdout:
            log_and_broadcast(line)
        proc.wait()
        if proc.returncode != 0:
            state.build_status = "failed"
            log_and_broadcast("CMake configuration failed!")
            return
            
        # 3. Compile
        log_and_broadcast("Compiling llama.cpp llama-server target...")
        cores = os.cpu_count() or 4
        build_compile_cmd = ["cmake", "--build", ".", "--config", "Release", "-j", str(cores), "--target", "llama-server"]
        
        proc = subprocess.Popen(build_compile_cmd, cwd=str(build_dir), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in proc.stdout:
            log_and_broadcast(line)
        proc.wait()
        if proc.returncode != 0:
            state.build_status = "failed"
            log_and_broadcast("Compilation failed!")
            return
            
        if BINARY_PATH.is_file():
            state.build_status = "success"
            log_and_broadcast("llama-server successfully compiled!")
        else:
            state.build_status = "failed"
            log_and_broadcast("Compilation finished, but llama-server binary was not found in the expected location.")
            
    except Exception as e:
        state.build_status = "failed"
        log_and_broadcast(f"Error occurred during build: {str(e)}")

def run_download_task(repo_id: str, filenames: list[str]):
    state.active_downloads[repo_id] = {
        "repo_id": repo_id,
        "filenames": filenames,
        "current_file": "",
        "downloaded_bytes": 0,
        "total_bytes": 0,
        "speed": 0.0,
        "progress": 0.0,
        "eta": 0,
        "status": "downloading",
        "error": None
    }
    
    total_sizes = {}
    downloaded_per_file = {f: 0 for f in filenames}
    
    # 1. Resolve files sizes first
    for fn in filenames:
        url = f"https://huggingface.co/api/models/{repo_id}/paths?path={fn}"
        try:
            r = requests.get(url, timeout=10)
            if r.status_code == 200:
                data = r.json()
                if data and isinstance(data, list):
                    total_sizes[fn] = data[0].get("size", 0)
        except Exception:
            pass
        if fn not in total_sizes:
            # Fallback to headers content-length check
            hf_url = f"https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/main/{fn}" # placeholder template
            hf_url = f"https://huggingface.co/{repo_id}/resolve/main/{fn}"
            try:
                r = requests.head(hf_url, allow_redirects=True, timeout=10)
                total_sizes[fn] = int(r.headers.get("content-length", 0))
            except Exception:
                total_sizes[fn] = 0

    total_all_files = sum(total_sizes.values())
    state.active_downloads[repo_id]["total_bytes"] = total_all_files
    
    try:
        start_time = time.time()
        for idx, filename in enumerate(filenames):
            state.active_downloads[repo_id]["current_file"] = filename
            hf_url = f"https://huggingface.co/{repo_id}/resolve/main/{filename}"
            target_path = MODELS_DIR / filename
            
            logger.info(f"Downloading {filename} from HF repo {repo_id}...")
            
            response = requests.get(hf_url, stream=True, allow_redirects=True, timeout=30)
            if response.status_code != 200:
                raise Exception(f"Failed to fetch model from HF. Status code: {response.status_code}")
                
            file_size = total_sizes.get(filename, int(response.headers.get("content-length", 0)))
            total_sizes[filename] = file_size
            
            # Recalculate total all files if some content length was not found initially
            total_all_files = sum(total_sizes.values())
            state.active_downloads[repo_id]["total_bytes"] = total_all_files
            
            chunk_size = 1024 * 1024  # 1MB chunks
            last_update = time.time()
            bytes_since_update = 0
            
            with open(target_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=chunk_size):
                    if not chunk:
                        continue
                    f.write(chunk)
                    chunk_len = len(chunk)
                    downloaded_per_file[filename] += chunk_len
                    state.active_downloads[repo_id]["downloaded_bytes"] = sum(downloaded_per_file.values())
                    bytes_since_update += chunk_len
                    
                    now = time.time()
                    elapsed = now - last_update
                    if elapsed >= 0.5: # Update progress every 500ms
                        overall_downloaded = state.active_downloads[repo_id]["downloaded_bytes"]
                        state.active_downloads[repo_id]["progress"] = round((overall_downloaded / total_all_files) * 100, 2) if total_all_files else 0.0
                        
                        speed = bytes_since_update / elapsed / (1024 * 1024) # MB/s
                        state.active_downloads[repo_id]["speed"] = round(speed, 2)
                        
                        remaining = total_all_files - overall_downloaded
                        total_elapsed = now - start_time
                        avg_speed = overall_downloaded / total_elapsed if total_elapsed > 0 else 1
                        state.active_downloads[repo_id]["eta"] = int(remaining / avg_speed) if avg_speed > 0 else 0
                        
                        # Push to SSE stream
                        for q in list(state.download_queues):
                            q.put_nowait(state.active_downloads[repo_id])
                            
                        last_update = now
                        bytes_since_update = 0
            
            # Make sure we show 100% on file complete
            overall_downloaded = sum(downloaded_per_file.values())
            state.active_downloads[repo_id]["downloaded_bytes"] = overall_downloaded
            state.active_downloads[repo_id]["progress"] = round((overall_downloaded / total_all_files) * 100, 2) if total_all_files else 100.0
            
        state.active_downloads[repo_id]["status"] = "success"
        state.active_downloads[repo_id]["speed"] = 0.0
        state.active_downloads[repo_id]["eta"] = 0
        for q in list(state.download_queues):
            q.put_nowait(state.active_downloads[repo_id])
            
    except Exception as e:
        logger.error(f"Download failed for {repo_id}: {str(e)}")
        state.active_downloads[repo_id]["status"] = "failed"
        state.active_downloads[repo_id]["error"] = str(e)
        for q in list(state.download_queues):
            q.put_nowait(state.active_downloads[repo_id])

# Pydantic schemas
class SetupRequest(BaseModel):
    cuda: bool

class ServerStartRequest(BaseModel):
    name: str
    model: str
    port: int
    host: str = "127.0.0.1"
    ngl: int = 99
    c: int = 2048

class ServerActionRequest(BaseModel):
    name: str

class DownloadRequest(BaseModel):
    repo_id: str
    filenames: list[str]

# API Endpoints
def check_binary_exists() -> bool:
    if BINARY_PATH.is_file():
        return True
    return shutil.which("llama-server") is not None

@app.get("/api/setup/status")
def get_setup_status() -> dict[str, Any]:
    binary_exists = check_binary_exists()
    cuda_available = check_cuda_available()
    return {
        "binary_exists": binary_exists,
        "cuda_available": cuda_available,
        "binary_path": str(BINARY_PATH),
        "llama_cpp_dir": str(LLAMA_CPP_DIR),
        "models_dir": str(MODELS_DIR),
        "build_status": state.build_status,
        "build_cuda": state.build_cuda
    }

@app.post("/api/setup/install")
def install_llamacpp(payload: SetupRequest, background_tasks: BackgroundTasks) -> dict[str, str]:
    if state.build_status == "building":
        raise HTTPException(status_code=400, detail="Installation is already in progress.")
    background_tasks.add_task(run_build_task, payload.cuda)
    return {"status": "started"}

@app.get("/api/setup/logs")
async def get_build_logs() -> StreamingResponse:
    queue = psutil.Queue() if hasattr(psutil, "Queue") else Any
    import asyncio
    queue = asyncio.Queue()
    state.build_queues.add(queue)
    
    async def log_stream():
        try:
            # Yield history first
            for log_line in state.build_logs:
                yield f"data: {log_line}\n\n"
            while True:
                line = await queue.get()
                yield f"data: {line}\n\n"
        finally:
            state.build_queues.discard(queue)
            
    return StreamingResponse(log_stream(), media_type="text/event-stream")

@app.get("/api/models/installed")
def list_installed_models() -> list[dict[str, Any]]:
    models = []
    
    # Scan both the user's explicit models dir and the HF cache dir
    hf_cache_dir = Path(os.path.expanduser("~/.cache/huggingface/hub"))
    scan_dirs = [MODELS_DIR]
    if hf_cache_dir.is_dir() and hf_cache_dir != MODELS_DIR:
        scan_dirs.append(hf_cache_dir)
        
    for base_dir in scan_dirs:
        for root, dirs, files in os.walk(base_dir, followlinks=True):
            for file in files:
                if not file.lower().endswith(".gguf"):
                    continue
                    
                p = Path(root) / file
                if not p.is_file():
                    continue
                
                stat = p.stat()
                # Parse quantization info from filename
                quant = "unknown"
                # Match typical gguf names containing Q4_K_M, Q8_0, F16, etc.
                match = re.search(r"([qQ]\d_[kK]_\w+|[qQ]\d_\d|[fF]\d+)", p.name)
                if match:
                    quant = match.group(1).upper()
                    
                try:
                    filename = str(p.relative_to(MODELS_DIR))
                except ValueError:
                    # If found in HF cache (outside MODELS_DIR)
                    filename = str(p)
                    
                models.append({
                    "filename": filename,
                    "path": str(p),
                    "size_bytes": stat.st_size,
                    "size_gb": round(stat.st_size / (1024 * 1024 * 1024), 2),
                    "quantization": quant,
                    "modified_time": stat.st_mtime
                })
    return sorted(models, key=lambda x: x["modified_time"], reverse=True)

@app.get("/api/models/search")
def search_hf_repo(repo: str) -> list[dict[str, Any]]:
    repo = repo.strip()
    if not repo or "/" not in repo:
        raise HTTPException(status_code=400, detail="Invalid repository name. Format should be 'author/repo_name' (e.g. 'Qwen/Qwen2.5-7B-Instruct-GGUF').")
        
    # Call HF API to get files inside repo
    url = f"https://huggingface.co/api/models/{repo}"
    try:
        r = requests.get(url, timeout=10)
        if r.status_code == 404:
            raise HTTPException(status_code=404, detail=f"HuggingFace repository '{repo}' not found.")
        elif r.status_code == 401:
            raise HTTPException(status_code=401, detail=f"Repository '{repo}' is private, requires authentication, or does not exist.")
            
        r.raise_for_status()
        data = r.json()
        
        gguf_files = []
        sibling_files = data.get("siblings", [])
        for sib in sibling_files:
            fn = sib.get("rfilename", "")
            if fn.endswith(".gguf"):
                # Get file size
                gguf_files.append({
                    "filename": fn,
                    "download_url": f"https://huggingface.co/{repo}/resolve/main/{fn}"
                })
        return gguf_files
    except requests.RequestException as e:
        raise HTTPException(status_code=500, detail=f"Failed to query HuggingFace API: {str(e)}")

@app.post("/api/models/download")
def download_model(payload: DownloadRequest, background_tasks: BackgroundTasks) -> dict[str, str]:
    if payload.repo_id in state.active_downloads and state.active_downloads[payload.repo_id]["status"] == "downloading":
        raise HTTPException(status_code=400, detail="This repository is already downloading.")
    background_tasks.add_task(run_download_task, payload.repo_id, payload.filenames)
    return {"status": "started"}

@app.get("/api/models/downloads")
def get_active_downloads() -> dict[str, Any]:
    return state.active_downloads

@app.get("/api/models/download/progress")
async def get_download_progress() -> StreamingResponse:
    import asyncio
    queue = asyncio.Queue()
    state.download_queues.add(queue)
    
    async def progress_stream():
        try:
            # Yield current active downloads on join
            yield f"data: {json.dumps(state.active_downloads)}\n\n"
            while True:
                update = await queue.get()
                yield f"data: {json.dumps(state.active_downloads)}\n\n"
        finally:
            state.download_queues.discard(queue)
            
    return StreamingResponse(progress_stream(), media_type="text/event-stream")

# Process Monitoring & Scanning
def scan_running_servers() -> dict[str, dict[str, Any]]:
    """Scan for llama-server instances running on the host system."""
    scanned_servers = {}
    for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'create_time']):
        try:
            cmd = proc.info['cmdline']
            # Match process name OR cmdline token
            is_llama = False
            if proc.info['name'] == 'llama-server':
                is_llama = True
            elif cmd and len(cmd) > 0 and 'llama-server' in cmd[0]:
                is_llama = True
                
            if is_llama:
                # Parse cmdline flags
                model = "unknown"
                port = 8080
                host = "127.0.0.1"
                ngl = 0
                c = 512
                
                # Simple cmdline parser
                i = 1
                while cmd and i < len(cmd):
                    token = cmd[i]
                    if token in ("-m", "--model") and i + 1 < len(cmd):
                        model = Path(cmd[i+1]).name
                        i += 2
                    elif token in ("-p", "--port") and i + 1 < len(cmd):
                        try:
                            port = int(cmd[i+1])
                        except ValueError:
                            pass
                        i += 2
                    elif token in ("--host",) and i + 1 < len(cmd):
                        host = cmd[i+1]
                        i += 2
                    elif token in ("-ngl", "--n-gpu-layers") and i + 1 < len(cmd):
                        try:
                            ngl = int(cmd[i+1])
                        except ValueError:
                            pass
                        i += 2
                    elif token in ("-c", "--ctx-size") and i + 1 < len(cmd):
                        try:
                            c = int(cmd[i+1])
                        except ValueError:
                            pass
                        i += 2
                    else:
                        i += 1
                
                # Derive status & uptime
                status = "running"
                if proc.status() == psutil.STATUS_ZOMBIE:
                    status = "crashed"
                    
                uptime = int(time.time() - proc.info['create_time'])
                
                # Create a name or port key
                server_name = f"discovered-{port}"
                
                # Cross-reference with our in-memory servers state
                # to preserve user-assigned names
                for k, v in state.servers.items():
                    if v.get("port") == port:
                        server_name = k
                        break
                        
                scanned_servers[server_name] = {
                    "name": server_name,
                    "pid": proc.pid,
                    "model": model,
                    "port": port,
                    "host": host,
                    "ngl": ngl,
                    "c": c,
                    "status": status,
                    "uptime": uptime,
                    "discovered": True
                }
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
            
    return scanned_servers

@app.get("/api/servers")
def list_servers() -> list[dict[str, Any]]:
    # Merge scanned processes and session started servers
    discovered = scan_running_servers()
    
    # Update state.servers with discovered parameters or keep them alive
    merged_list = []
    
    # 1. Update status of existing servers in state
    for name, s_info in list(state.servers.items()):
        pid = s_info.get("pid")
        if name in discovered:
            # Sync details from OS discovery
            state.servers[name]["status"] = discovered[name]["status"]
            state.servers[name]["uptime"] = discovered[name]["uptime"]
            state.servers[name]["pid"] = discovered[name]["pid"]
            merged_list.append(state.servers[name])
        else:
            # Not found in OS process list
            # If it was supposed to be running, it has crashed
            if s_info["status"] == "running":
                state.servers[name]["status"] = "crashed"
                state.servers[name]["uptime"] = 0
            merged_list.append(s_info)
            
    # 2. Add newly discovered ones not in state
    for name, s_info in discovered.items():
        if name not in state.servers:
            # Add to state tracking
            state.servers[name] = s_info
            merged_list.append(s_info)
            
    return merged_list

@app.post("/api/servers/start")
def start_server(payload: ServerStartRequest) -> dict[str, Any]:
    # 1. Validations
    bin_path = str(BINARY_PATH)
    if not BINARY_PATH.is_file():
        # Fallback to system llama-server
        sys_path = shutil.which("llama-server")
        if sys_path:
            bin_path = sys_path
        else:
            raise HTTPException(status_code=400, detail="llama-server binary is not compiled. Please install/build it first.")
        
    if payload.name in state.servers and state.servers[payload.name]["status"] == "running":
        raise HTTPException(status_code=400, detail=f"Server with name '{payload.name}' is already running.")
        
    if is_port_in_use(payload.port):
        # Double check if port is currently registered by a running server
        raise HTTPException(status_code=400, detail=f"Port {payload.port} is already in use by another application.")
        
    model_path = MODELS_DIR / payload.model
    if not model_path.is_file():
        raise HTTPException(status_code=400, detail=f"Model file '{payload.model}' not found in models directory.")
        
    # 2. Build command
    cmd = [
        bin_path,
        "-m", str(model_path),
        "--port", str(payload.port),
        "--host", payload.host,
        "-ngl", str(payload.ngl),
        "-c", str(payload.c)
    ]
    
    # Add metrics endpoint activation if supported in build
    # (llama.cpp enables /metrics when we pass no flag, it's ON by default now in llama-server)
    
    # 3. Launch process
    log_file = LOGS_DIR / f"{payload.name}.log"
    try:
        f_log = open(log_file, "w")
        proc = subprocess.Popen(
            cmd,
            stdout=f_log,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True # Detach so it survives backend restarts
        )
        
        # Give it 1 second to start and check if it failed immediately
        time.sleep(1)
        proc.poll()
        status = "running"
        if proc.returncode is not None:
            status = "crashed"
            
        state.servers[payload.name] = {
            "name": payload.name,
            "pid": proc.pid,
            "model": payload.model,
            "port": payload.port,
            "host": payload.host,
            "ngl": payload.ngl,
            "c": payload.c,
            "status": status,
            "uptime": 0,
            "started_at": time.time(),
            "discovered": False
        }
        
        return {"status": "started", "server": state.servers[payload.name]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to start llama-server process: {str(e)}")

@app.post("/api/servers/stop")
def stop_server(payload: ServerActionRequest) -> dict[str, Any]:
    if payload.name not in state.servers:
        raise HTTPException(status_code=404, detail=f"Server '{payload.name}' not found.")
        
    server = state.servers[payload.name]
    pid = server.get("pid")
    
    if not pid:
        state.servers[payload.name]["status"] = "stopped"
        return {"status": "stopped", "message": "Server was not running."}
        
    try:
        proc = psutil.Process(pid)
        # Verify it is indeed llama-server
        if "llama-server" in " ".join(proc.cmdline()):
            logger.info(f"Terminating llama-server process with PID {pid}...")
            proc.terminate() # Clean SIGTERM
            try:
                proc.wait(timeout=5)
            except psutil.TimeoutExpired:
                logger.warn(f"Process PID {pid} did not stop. Killing...")
                proc.kill() # SIGKILL
        else:
            logger.warn(f"PID {pid} exists but does not match llama-server cmdline. Skipping.")
    except psutil.NoSuchProcess:
        pass
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to stop process: {str(e)}")
        
    state.servers[payload.name]["status"] = "stopped"
    state.servers[payload.name]["pid"] = None
    state.servers[payload.name]["uptime"] = 0
    
    return {"status": "stopped", "server": state.servers[payload.name]}

@app.post("/api/servers/restart")
def restart_server(payload: ServerActionRequest) -> dict[str, Any]:
    if payload.name not in state.servers:
        raise HTTPException(status_code=404, detail=f"Server '{payload.name}' not found.")
        
    server = state.servers[payload.name]
    
    # 1. Stop if running
    if server["status"] == "running":
        stop_server(payload)
        
    # 2. Re-start with saved parameters
    start_req = ServerStartRequest(
        name=server["name"],
        model=server["model"],
        port=server["port"],
        host=server["host"],
        ngl=server["ngl"],
        c=server["c"]
    )
    return start_server(start_req)

@app.get("/api/servers/logs/{name}")
def get_server_logs(name: str, lines: int = 100) -> dict[str, Any]:
    log_file = LOGS_DIR / f"{name}.log"
    
    # Check if this server is a discovered server without local log file
    if not log_file.is_file():
        # See if we can find it in state. If discovered, it might not have logs
        if name in state.servers:
            return {
                "name": name,
                "lines": [],
                "error": "This server was started externally (auto-discovered). Output logs are not captured by this manager."
            }
        raise HTTPException(status_code=404, detail="Log file not found.")
        
    try:
        # Read last N lines using tail utility or python file read
        with open(log_file, "r") as f:
            content = f.readlines()
        tail = content[-lines:] if len(content) > lines else content
        return {
            "name": name,
            "lines": [line.rstrip("\n") for line in tail]
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read logs: {str(e)}")

@app.get("/api/servers/metrics/{name}")
def get_server_metrics(name: str) -> dict[str, Any]:
    if name not in state.servers:
        raise HTTPException(status_code=404, detail="Server not found.")
    
    server = state.servers[name]
    if server["status"] != "running":
        return {"status": "offline", "tokens_sec": 0.0}
        
    port = server["port"]
    # Check llama-server's metrics API
    metrics_url = f"http://127.0.0.1:{port}/metrics"
    try:
        r = requests.get(metrics_url, timeout=1.5)
        if r.status_code == 200:
            lines = r.text.split("\n")
            tokens_sec = 0.0
            # llama.cpp metrics: looking for output token count or rate
            # In modern llama-server: llama_prompt_tokens_seconds or llama_tokens_seconds etc.
            # Let's inspect metrics lines
            for line in lines:
                if line.startswith("llama_eval_sec") or line.startswith("llama_tokens_seconds"):
                    parts = line.split()
                    if len(parts) >= 2:
                        tokens_sec = float(parts[-1])
            return {"status": "online", "tokens_sec": round(tokens_sec, 2)}
    except Exception:
        pass
        
    # Fallback to tailing logs for token count
    # llama.cpp outputs "eval time = ... ms ( ... tokens per second)"
    log_file = LOGS_DIR / f"{name}.log"
    if log_file.is_file():
        try:
            with open(log_file, "r") as f:
                content = f.read()
            matches = re.findall(r"(\d+\.\d+)\s+tokens\s+per\s+second", content)
            if matches:
                return {"status": "online", "tokens_sec": float(matches[-1])}
        except Exception:
            pass
            
    return {"status": "online", "tokens_sec": 0.0}

@app.get("/api/network/info")
def get_network_info() -> dict[str, Any]:
    return {
        "lan_ips": get_lan_ips()
    }

# Mount static folder
STATIC_DIR = Path(__file__).parent / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

@app.get("/")
def get_index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8090, reload=True)

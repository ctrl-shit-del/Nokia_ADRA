# llama.cpp Manager — GUI Build Prompt

## Goal

Build a small local web app (FastAPI + a lightweight frontend — plain HTML/JS is fine,
doesn't need React) that lets a non-terminal user fully manage llama.cpp on their
machine: install/build llama.cpp, browse and download GGUF models from HuggingFace,
and start/stop/monitor one or more llama-server processes — all from a browser, zero
terminal commands after initial setup.

This is a **process manager + model downloader**, not a chat UI (llama-server already
has its own chat UI built in at `http://host:port` once running — don't rebuild that).

## Why this matters for the ADRA use case

ADRA's backend already supports multiple named llama-server endpoints
(`ADRA_MODEL_ENDPOINTS` in `adra_common.py`, `/api/models/register` and
`/api/models/select` routes). This tool is what *creates* those endpoints in the first
place — someone should be able to click "download Qwen2.5-7B", click "start on port
8080", and have it immediately show up as a selectable option in ADRA's model dropdown,
without ever opening a terminal.

## Screens

### 1. Setup / Install Check
On first load, detect whether `llama-server` binary exists at a configurable path
(default: `~/llama.cpp/build/bin/llama-server`). If missing:
- Show install instructions with a one-click "Install llama.cpp" button that runs the
  build steps (`git clone`, `cmake`, `cmake --build`) as a background job, streaming
  build output live (Server-Sent Events) to a terminal-style log panel in the browser.
- Detect CUDA availability (`nvidia-smi` presence) and pre-check the
  `-DGGML_CUDA=ON` build flag if a GPU is found; explain the tradeoff in one sentence
  ("with GPU: much faster, needs CUDA toolkit; without: works everywhere, CPU-only").
- Show a progress bar and estimated time remaining if possible (the build has
  predictable phases: configure ~10s, compile ~2-4min depending on cores).

### 2. Model Library
A searchable list of models, split into two sections:

**Installed models** (scanned from a configurable models directory, default `~/models/`):
- Card per model: filename, size on disk, quantization level (parsed from filename,
  e.g. `Q4_K_M`), and a "Run" button.

**Browse HuggingFace** (search box + curated quick-picks):
- Quick-pick buttons for a short curated list suited to consumer hardware (don't make
  the user guess quantization): Qwen2.5-7B-Instruct, Qwen2.5-14B-Instruct,
  Qwen3-8B-Instruct, Llama-3.1-8B-Instruct — each pre-selected at Q4_K_M with a one-line
  RAM estimate ("~4.5GB, fits in 8GB+ RAM machines").
- A raw search box for any HuggingFace repo (`org/repo-name`) for advanced users, which
  lists available `.gguf` files in that repo with their sizes so the user picks the
  right quantization manually.
- Click "Download" → background job with a live progress bar (bytes downloaded / total,
  MB/s, ETA) via SSE — `huggingface_hub`'s `hf_hub_download` supports a progress
  callback, wire that to the SSE stream.
- Handle multi-part GGUF files (some large models ship split as
  `model-00001-of-00003.gguf` etc.) — download all parts, show combined progress.

### 3. Running Servers
This is the process-management core.

- Table of currently running llama-server processes: name/label, model file, port, host
  binding (`127.0.0.1` vs `0.0.0.0`), GPU layers (`-ngl`), context size (`-c`), status
  (starting/running/crashed), uptime, and a live token/sec estimate if obtainable from
  llama-server's own metrics endpoint (`/metrics` if built with `-DLLAMA_METRICS=ON`, or
  parse from stdout).
- "Start new server" form: pick an installed model from a dropdown, set a friendly name,
  port (auto-suggest next free port starting from 8080), GPU layers slider (0 to "max" —
  detect total layers from the gguf metadata if possible, otherwise default to 99 for
  "offload everything"), context size, and a host toggle: "This machine only
  (127.0.0.1)" vs "Allow other machines on my network (0.0.0.0)" — explain the second
  option in one sentence since it's the two-PC setup case ("other computers on your
  network, like a machine running ADRA, will be able to connect to this model").
- Start/Stop/Restart buttons per running server. Stop should be a clean SIGTERM with a
  timeout before SIGKILL.
- Live log tail per server (SSE or WebSocket), collapsible, auto-scrolling, with a
  "copy last 50 lines" button for bug reports.
- Crash detection: if a server process dies unexpectedly (non-zero exit, not from a user
  Stop action), surface a toast/banner with the last few lines of stderr — this is where
  users usually get stuck silently (OOM kills, port conflicts, corrupt gguf).

### 4. Network / Sharing panel
Specifically for the two-PC scenario:
- Show this machine's LAN IP address(es) automatically (don't make the user run
  `ip addr` — get it from Python's `socket` module).
- For each running server bound to `0.0.0.0`, show a copy-paste-ready connection string:
  `http://<lan-ip>:<port>/v1/chat/completions` — this is exactly what someone would set
  as `ADRA_LLAMACPP_URL` or register via ADRA's `/api/models/register`.
- A "Test from here" button that does a local curl-equivalent to confirm the server
  actually responds, separate from "is it reachable from other machines" (which requires
  a note: firewall rules on this machine may still block LAN access even if the server
  itself is running — link to a one-liner for opening the port on ufw if detected:
  `sudo ufw allow <port>/tcp`).

## Technical requirements

- **Backend:** FastAPI, using `subprocess.Popen` to manage llama-server child processes
  (not `os.system` — need the process handle to stop/monitor it). Store running process
  state in memory (a dict of name → Popen handle + metadata) since this tool's own
  lifetime is short (user opens it, manages servers, closes it — the servers themselves
  keep running as detached background processes even if this manager GUI is closed,
  since they're separate OS processes).
- **Process persistence:** on manager startup, scan for already-running llama-server
  processes (e.g. via `psutil.process_iter()` matching on process name/cmdline) so a
  server started in a previous session still shows up — don't assume the manager is the
  only thing that can start/stop these.
- **Model downloads:** use `huggingface_hub.hf_hub_download` with a `tqdm`-compatible
  progress callback adapted to push updates through an SSE queue, same pattern as
  ADRA's own token-usage SSE.
- **Frontend:** plain HTML + vanilla JS + Tailwind via CDN is sufficient — this doesn't
  need React's complexity, it's a handful of screens with polling/SSE updates, not a
  complex interactive app.
- **No terminal required after first install:** the ONE place a terminal is unavoidable
  is the very first `git clone` + `cmake` build, and only if the user doesn't already
  have llama.cpp built — even that should be triggerable from the browser via the
  Setup screen's background-job pattern described above.
- **Safety:** validate the port isn't already in use before starting a server (return a
  clear error, don't let two servers silently fight over one port). Validate the model
  file path is inside the configured models directory (don't let a malicious/malformed
  request start a server against an arbitrary file path).

## Explicit non-goals

- Don't rebuild a chat interface — llama-server's own web UI at `http://host:port`
  already does this well.
- Don't attempt to manage remote llama-server instances on other machines from this
  GUI — this tool manages processes on the machine it's running on. Cross-machine
  awareness is just the "here's your LAN IP, here's the connection string" panel, not
  remote process control.
- Don't build model fine-tuning, quantization-from-scratch, or LoRA merging — this is
  purely: install the runtime, download pre-quantized GGUFs, run/stop server processes.

## Suggested build order

1. Running Servers screen first (start/stop/list/logs) — this is the core value and
   works once llama.cpp is already built, so you can develop against a manually-built
   llama-server while the Setup screen is still being built.
2. Model Library screen (scan installed + HuggingFace download).
3. Network/Sharing panel (mostly read-only info display, quick to build).
4. Setup/Install Check screen last, since it's the least-used path (most users build
   llama.cpp once and never revisit this screen).

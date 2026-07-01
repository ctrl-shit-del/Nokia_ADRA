# Nokia ADRA (Autonomous Deployment Readiness Agent)

## Overview
ADRA is a multi-agent, LLM-powered system designed to autonomously assess, prepare, and validate Linux servers for deploying the Nokia Aurelis Command Center. Developed in collaboration with VIT Chennai, it replaces manual, error-prone server audits with an intelligent, self-correcting pipeline.

The system uses specialized, fail-safe Python agents. When a command fails, the agents read the error output, consult a local LLM to diagnose the root cause, and attempt progressively refined recovery commands (up to 5 retry cycles) before proceeding.

## Core Pipeline & Architecture
The deployment pipeline consists of four sequential agents:

1. **`requirements_agent.py`**: Extracts hardware and software prerequisites from Nokia Aurelis documentation using RAG (Chroma DB + sentence-transformers) and LLM parsing to create a structured `requirements.json`.
2. **`inventory_agent.py`**: Connects via SSH to the target server, runs an LLM-driven planner loop to gather system specs, and creates an `inventory.json` gap table comparing required vs. found items (Met / Missing / Insufficient).
3. **`packages_agent.py`**: Installs missing/insufficient software using OS-specific YAML skills. Features a robust LLM retry loop to recover from setup and package manager errors dynamically. Updates defaults to `inventory.json` upon success.
4. **`installer_agent.py`**: Runs the Nokia Aurelis installation script sequentially over SSH. Recovers from step failures using the LLM and runs a post-installation verification suite (generating `session_report.md`).

## Data & State
- **Local LLM Layer**: Powered by `llama-server` through its OpenAI-compatible `/v1/chat/completions` endpoint. Set `ADRA_LLAMACPP_URL` and `ADRA_LLAMACPP_MODEL` to override the defaults. **New:** If the LLM server is unavailable, the system gracefully degrades to a mock-model mode, ensuring the UI remains functional for demonstration and testing.
- **llama.cpp Manager**: A self-contained local web application (`llamacpp_manager/`) that automates compiling `llama-server` (with/without CUDA), downloading GGUF models directly from HuggingFace (supporting split files), and starting, stopping, and monitoring running `llama-server` instances. Discovered or started models can be registered directly with ADRA.
- **Audit Database**: A local SQLite database (`adra_audit.db`) logs every command, SSH output, and LLM exchange (diagnostic reasonings, commands chosen) for complete traceability and debugging.
- **Requirement Profiles**: Known/custom versions are stored in `requirement_profiles.db`; selecting a known profile writes `requirements.json` without an LLM or vector DB call.
- **Vector DB**: Chroma DB (stored in `db/chroma_db/`) used for document embedding and RAG queries.
- **Skills Library**: Package installation skills live in `skills/*.yaml`; learned fixes are appended to these files after successful LLM-guided recovery.

## Setup & Running

**1. Environment Setup:**
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

**2. Start the Local or Remote LLM:**
Ensure you have an OpenAI-compatible endpoint running. You can launch `llama-server` on your current machine or a separate dedicated machine with a better GPU.

**Option A: Running Locally**
```bash
llama-server \
    -m /home/mystic/models/qwen25/qwen2.5-7b-instruct-q5_k_m-00001-of-00002.gguf \
    -t 16 \
    -c 8192 \
    --port 8080
```

**Option B: Running on a Remote PC (with a Better GPU)**
If you have another PC with a stronger GPU (e.g., an RTX 3090/4090 or multiple GPUs), you can run the model there to significantly speed up ADRA's reasoning!
1. Start `llama-server` on the powerful PC and bind it to its network IP using `--host`:
   ```bash
   llama-server -m /path/to/better-model.gguf --host 0.0.0.0 --port 8080 -c 8192 -ngl 99
   ```
   *(Note: `-ngl 99` offloads all layers to the GPU for maximum speed).*
2. On the machine running ADRA, simply set the `ADRA_LLAMACPP_URL` environment variable to point to the powerful PC's IP address before running uvicorn:
   ```bash
   export ADRA_LLAMACPP_URL="http://<powerful-pc-ip>:8080/v1/chat/completions"
   export ADRA_LLAMACPP_MODEL="<better-model-name>" # Optional: matches the model loaded
   ```

*Note: If no LLM is detected, ADRA will automatically fall back to mock JSON responses to keep the application running.*

**3. Run the Web UI/API (Recommended):**
ADRA features a fully-integrated, React-based Command Center UI powered by FastAPI.
```bash
uvicorn backend:app --reload
```
Navigate to `http://127.0.0.1:8000` to access the ADRA Command Center. The UI is fully connected to the backend API, allowing you to orchestrate the Agents directly from your browser:
* **Requirements Tab:** Dynamically fetch version profiles or upload custom HTML/PDF documentation to trigger the RAG pipeline.
* **Inventory Tab:** Enter target SSH credentials to trigger live server gap analysis.
* **Packages Tab:** Review required package updates and acknowledge hardware risks based on actual `inventory.json` state.
* **Installer Tab:** Trigger the full installation flow and watch live agent execution logs, step checklists, and active console output tailing in real-time.

**4. Run the llama.cpp Manager GUI:**
Manage compiling llama.cpp, downloading GGUF models directly from HuggingFace, and starting/monitoring llama-server instances:
```bash
uvicorn llamacpp_manager.main:app --host 0.0.0.0 --port 8090 --reload
```
Navigate to `http://127.0.0.1:8090` to start managing models.

**5. Run Agents via CLI (Alternative):**
You can also run the pipeline sequentially via the command line:
```bash
python3 requirements_agent.py
python3 inventory_agent.py
python3 packages_agent.py
python3 installer_agent.py
```
*Note: The CLI agents will prompt you for the SSH credentials of the target server upon execution and feature interactive prompts to override deployment blockers.*

**6. View Audit Logs & Output:**
Query the SQLite database to trace the LLM's automated diagnostic and repair steps:
```bash
sqlite3 adra_audit.db "SELECT item, attempt_num, status, content FROM events WHERE agent='packages_agent' ORDER BY id;"
```

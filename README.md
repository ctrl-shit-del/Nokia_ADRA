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

**2. Start the Local LLM:**
Ensure you have an OpenAI-compatible endpoint running (e.g., using `llama-server`).
*Set `ADRA_LLAMACPP_URL` and `ADRA_LLAMACPP_MODEL` environment variables if your setup differs from the defaults.*
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
* **Installer Tab:** Trigger the full installation flow and watch live agent execution logs and LLM diagnoses stream in real-time.

**4. Run Agents via CLI (Alternative):**
You can also run the pipeline sequentially via the command line:
```bash
python3 requirements_agent.py
python3 inventory_agent.py
python3 packages_agent.py
python3 installer_agent.py
```
*Note: The CLI agents will prompt you for the SSH credentials of the target server upon execution and feature interactive prompts to override deployment blockers.*

**5. View Audit Logs & Output:**
Query the SQLite database to trace the LLM's automated diagnostic and repair steps:
```bash
sqlite3 adra_audit.db "SELECT item, attempt_num, status, content FROM events WHERE agent='packages_agent' ORDER BY id;"
```

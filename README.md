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
- **Local LLM Layer**: Powered by Ollama (Qwen3, Mistral, or Gemma 4) ensuring no data leaves the internal network.
- **Audit Database**: A local SQLite database (`adra_audit.db`) logs every command, SSH output, and LLM exchange (diagnostic reasonings, commands chosen) for complete traceability and debugging.
- **Vector DB**: Chroma DB (stored in `db/chroma_db/`) used for document embedding and RAG queries.

## Setup & Running

**1. Environment Setup:**
```bash
python3 -m venv venv
source venv/bin/activate
pip install paramiko requests  # Ensure required libraries are met
```

**2. Run the Pipeline Sequentially:**
```bash
python3 requirements_agent.py
python3 inventory_agent.py
python3 packages_agent.py
python3 installer_agent.py
```
*Note: The packages and installer agents will prompt you for the SSH credentials of the target server upon execution. They also feature interactive prompts to override deployment blockers.*

**3. View Audit Logs & Output:**
Query the SQLite database to trace the LLM's automated diagnostic and repair steps:
```bash
sqlite3 adra_audit.db "SELECT item, attempt_num, status, content FROM events WHERE agent='packages_agent' ORDER BY id;"
```

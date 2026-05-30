# Nokia ADRA

## Overview
This project is an agent-based system focused on automated requirements processing, package evaluation, and inventory auditing. It uses a set of specialized Python conversational/task agents to audit and track system states, utilizing both relational databases (SQLite) and vector databases (Chroma DB).

## Core Components
- **`inventory_agent.py`**: Manages and tracks inventory items/states.
- **`packages_agent.py`**: Handles and verifies package information.
- **`requirements_agent.py`**: Parses and validates requirements (e.g., against `mock_aurelis_doc.txt` or JSON formats).

## Data & State
- **Audit Database**: Local SQLite databases (like `adra_audit.db`) check and persist agent operations, attempt numbers, statuses, and outputs.
- **Vector DB**: Chroma DB is used (stored in `db/chroma_db/`) for embedding and similarity search processes across documentation.

## Setup & Running
1. Activate the environment:
   ```bash
   source venv/bin/activate
   ```
2. Run an agent, for example:
   ```bash
   python3 packages_agent.py
   ```

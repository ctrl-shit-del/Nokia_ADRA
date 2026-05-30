# Product Requirements Document
## ADRA — Autonomous Deployment Readiness Agent
### Nokia Aurelis Command Center · VIT Chennai Collaboration

---

**Version:** 1.0  
**Date:** May 2026  
**Status:** Draft — Ready for Review  
**Team:** VIT Chennai (SENSE Department) × Nokia  
**Primary Language:** Python  
**Target Product:** Nokia Aurelis Command Center (Release R25.12)

---

## 1. Executive Summary

ADRA is a multi-agent, LLM-powered system that autonomously assesses, prepares, and validates a Linux server for deploying the Nokia Aurelis Command Center. It replaces manual server audits — which are time-consuming, error-prone, and require deep product knowledge — with an intelligent, self-correcting pipeline of four specialized agents: Requirements Extraction, Inventory, Packages, and Installer.

Each agent is fail-safe: when any command fails, the agent reads the error output, consults the LLM (up to 5 retry cycles), and attempts progressively refined recovery. Every agent interaction, command, output, and LLM exchange is written to a persistent SQLite audit log that can be searched and reviewed after the fact.

---

## 2. Problem Statement

Deploying Nokia Aurelis Command Center on a Linux server requires:

- Reading dozens of pages of hardware and software prerequisites across multiple documentation releases
- Manually SSHing into the target server and verifying CPU cores, RAM, storage, kernel version, package versions, and Kubernetes status
- Installing or upgrading 10–30 software packages in the correct order, with OS-specific commands
- Executing an Aurelis installation script that has multiple failure modes, many of which require knowing how to interpret error messages and apply specific fixes

This process takes 2–8 hours per server, requires senior telecom infrastructure expertise, and fails silently in ways that are hard to diagnose later. ADRA automates the entire pipeline and makes every decision traceable.

---

## 3. Goals and Non-Goals

### Goals

- Automatically extract structured hardware and software requirements from Nokia Aurelis documentation using RAG and LLM-based parsing
- Connect to a Linux target server via SSH and produce a complete inventory of hardware specs and installed packages
- Compare the inventory against requirements and identify every gap
- Install all missing or outdated packages with OS-appropriate commands, recovering from errors using LLM diagnosis
- Execute the Aurelis installation script sequentially, recovering from failures using LLM diagnosis
- Maintain a full audit trail of every command, output, and LLM exchange in a searchable SQLite database
- Notify the user immediately and clearly if any step exhausts all retry cycles

### Non-Goals

- Managing Aurelis post-installation configuration (day-2 operations)
- Supporting non-Linux operating systems (Windows, macOS)
- Replacing Nokia's official Aurelis documentation
- Full network topology validation beyond the single target server
- Multi-server parallel deployment orchestration (future version)

---

## 4. System Architecture Overview

```
Nokia Aurelis Docs (HTML/PDF)
        │
        ▼
┌───────────────────────────────────────┐
│  Requirements Extraction Agent        │ ◀──▶  Vector DB (RAG)
│  LLM parses docs → structured JSON   │
└──────────────────┬────────────────────┘
                   │ Structured Requirements JSON
                   ▼
┌───────────────────────────────────────┐
│  Inventory Agent                      │ ◀──▶  Linux Target Server (SSH)
│  SSH planner loop → inventory table  │
└──────────────────┬────────────────────┘
                   │ Inventory Table (Met / Missing)
                   ▼
┌───────────────────────────────────────┐
│  Packages Agent                       │ ◀──▶  YAML Skills Library
│  Install missing → LLM error recovery│ ◀──▶  LLM Reasoning Layer (≤5 retries)
└──────────────────┬────────────────────┘
                   │
                   ▼
┌───────────────────────────────────────┐
│  Installer Agent                      │ ◀──▶  LLM Reasoning Layer (≤5 retries)
│  Run Aurelis script → recovery        │
└──────────────────┬────────────────────┘
                   │
          ┌────────┴────────┐
          ▼                 ▼
   Deployment Complete   User Alert
   (Aurelis running ✓)  (Retries exhausted)

All agents → SQLite Audit Log → Session Report
```

**LLM Layer:** Ollama runtime, local deployment. Supported models: Qwen3, Mistral, Gemma 4. All models run on a dedicated LLM server (separate from the Aurelis target server).

**UI:** Streamlit web application with tabs for Requirements, Inventory, Packages, Installer, and Logs.

---

## 5. Agent Specifications

### 5.1 Requirements Extraction Agent

**Role:** Convert Nokia Aurelis installation documentation into a structured, machine-readable requirements object. This is the only agent that touches the documentation source; all downstream agents consume its JSON output.

**Inputs:**
- Nokia Aurelis Command Center installation documentation (HTML or PDF, release R25.12 and variants)
- Previously stored YAML skill definitions (for incremental runs)

**Process:**
1. Load and chunk the documentation into overlapping text segments
2. Embed chunks using a sentence-transformer model and store in a vector database (ChromaDB or FAISS)
3. Issue RAG queries for key requirement categories: hardware minimums (CPU cores, RAM, storage type and size), OS version, kernel version, required software packages and their minimum versions, network configuration requirements, and Kubernetes/container runtime specifications
4. For each retrieved chunk, prompt the LLM to extract structured data in JSON format, specifying field names, units, and comparison operators (e.g., `{"ram_gb": {"min": 32, "op": "gte"}}`)
5. Merge and deduplicate the extracted requirements into a single canonical requirements object
6. For any software category not already covered by an existing YAML skill definition, prompt the LLM to auto-generate a new YAML skill entry (with check, install, and verify commands)
7. Persist the requirements object to SQLite and write any new YAML skills to the skills library

**Outputs:**
- `requirements.json` — structured requirements object with hardware thresholds and software version constraints
- Updated YAML skills library (if new categories were discovered)
- SQLite audit entries for all LLM calls and extracted data

**Output Schema (example):**
```json
{
  "hardware": {
    "cpu_cores_min": 12,
    "ram_gb_min": 32,
    "disk_gb_min": 200,
    "disk_type": "SSD",
    "network_interfaces_min": 2
  },
  "software": {
    "python": {"min_version": "3.10", "package_manager": "pip"},
    "docker": {"min_version": "20.10"},
    "kubernetes": {"min_version": "1.26", "runtime": "containerd"},
    "helm": {"min_version": "3.10"}
  },
  "os": {
    "family": ["ubuntu", "rhel", "centos"],
    "min_kernel": "5.15"
  }
}
```

**Error handling:** If an LLM extraction call returns malformed JSON, retry up to 3 times with a stricter prompt. If extraction fails after retries, log the failure and proceed with any partial requirements already collected; flag the incomplete extraction in the report.

---

### 5.2 Inventory Agent

**Role:** Connect to the target Linux server via SSH and produce a complete inventory of its current hardware and software state. Compare this inventory against the requirements JSON to produce a gap table (Met / Missing / Insufficient).

**Inputs:**
- `requirements.json` from the Requirements Extraction Agent
- SSH credentials for the target server (hostname, username, private key or password)

**Process:**
1. Establish an SSH connection to the target server using Paramiko
2. Run an LLM-driven planner loop: given the current requirements, the LLM decides which shell command to run next to gather information (e.g., `nproc`, `free -h`, `df -h`, `python3 --version`, `docker version`, `kubectl version`, `uname -r`)
3. Execute each command over SSH, capture stdout and stderr
4. Feed the output back to the LLM for interpretation and to determine whether more commands are needed
5. Continue until all requirement fields have corresponding inventory values
6. Build the inventory table by comparing each requirement against the observed value
7. Classify each item as Met, Missing (package not found), or Insufficient (package found but below minimum version)
8. Persist the inventory table to SQLite

**Outputs:**
- `inventory.json` — full inventory table with required values, found values, and Met/Missing/Insufficient status for each item
- SQLite audit entries for all SSH commands and their outputs

**Output Schema (example):**
```json
{
  "hardware": [
    {"item": "cpu_cores",    "required": 12,    "found": 16,    "status": "Met"},
    {"item": "ram_gb",       "required": 32,    "found": 16,    "status": "Insufficient"},
    {"item": "disk_gb",      "required": 200,   "found": 500,   "status": "Met"},
    {"item": "disk_type",    "required": "SSD", "found": "HDD", "status": "Insufficient"}
  ],
  "software": [
    {"item": "python",     "required": ">=3.10", "found": "3.8.10",  "status": "Insufficient"},
    {"item": "docker",     "required": ">=20.10","found": "20.10.21","status": "Met"},
    {"item": "kubernetes", "required": ">=1.26", "found": null,      "status": "Missing"}
  ]
}
```

**Error handling:** If an SSH command returns a non-zero exit code, pass the output to the LLM to determine whether to retry with a different command or mark the item as undetermined. SSH connection failures cause the agent to abort and notify the user immediately with the connection error details.

---

### 5.3 Packages Agent

**Role:** Install all software packages classified as Missing or Insufficient by the Inventory Agent. Use YAML skill definitions to drive installation commands, and recover from errors using LLM diagnosis.

**Inputs:**
- `inventory.json` from the Inventory Agent (filtered to Missing and Insufficient software items)
- YAML skills library (each skill defines check, install, and verify commands for one software category)
- Detected OS flavor (from Inventory Agent: ubuntu/debian vs rhel/centos/fedora)

**Process:**
1. For each Missing or Insufficient software item:
   a. Look up the corresponding YAML skill definition
   b. Run the **check command** to confirm current state (the inventory may be stale by the time this agent runs)
   c. If the item is confirmed missing or outdated, run the **install command** appropriate for the detected OS flavor
   d. If the install command succeeds (exit code 0), run the **verify command** to confirm the correct version is now active
   e. If the install command fails, enter the **LLM retry loop** (see below)
2. After all items are processed, log a summary of installed, failed, and skipped items

**LLM retry loop (up to 5 total attempts):**

Each attempt follows this protocol:
- Capture the full command output (stdout + stderr) and the exit code
- Build a prompt containing: the software item being installed, the OS flavor, the failed command, the complete error output, and a structured history of all previous attempts and their outcomes in this session
- Ask the LLM to: (1) diagnose the root cause of the failure, (2) suggest the next command to run, and (3) explain the reasoning
- Execute the LLM-suggested command
- If it succeeds, run the verify command and exit the loop
- If it fails, capture the new error, update the history, and proceed to the next attempt (up to 5 total)
- Each attempt addresses the specific error from the previous attempt — not a repetition of the same command
- After 5 failed attempts, log the full retry history, mark the item as failed, and continue to the next package

**Important:** The retry counter is scoped to the current error chain, not to the overall installation. If attempt 1 fails with a GPG key error and attempt 2 fixes that but introduces a dependency conflict, attempt 3 addresses the dependency conflict — not the original GPG error.

**YAML Skill Schema:**
```yaml
docker:
  description: "Docker container runtime"
  check:
    command: "docker --version"
    parse_version: "Docker version (\\S+),"
  install:
    ubuntu: "apt-get install -y docker-ce docker-ce-cli containerd.io"
    rhel: "yum install -y docker-ce docker-ce-cli containerd.io"
    pre_commands:
      ubuntu:
        - "apt-get update"
        - "curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /usr/share/keyrings/docker-archive-keyring.gpg"
  verify:
    command: "docker --version && systemctl is-active docker"
    expected_exit_code: 0
```

**Outputs:**
- Updated `inventory.json` with installation results
- SQLite audit entries for every command, every LLM prompt, every LLM response, and the final outcome of each package

**Error handling:** If all 5 retry attempts for a package fail, the agent does not abort — it marks the package as failed, logs the full retry history (including all LLM conversations), and continues to the next package. The Installer Agent is not started if any required package is still in a failed state; it waits for user confirmation or remediation.

---

### 5.4 Installer Agent

**Role:** Execute the Nokia Aurelis installation script sequentially, verify each step, and recover from failures using LLM diagnosis. This is the final agent in the pipeline.

**Inputs:**
- Confirmation that all required packages are either Met or have been successfully installed by the Packages Agent
- The Aurelis installation script and its expected execution sequence
- SSH credentials for the target server

**Process:**
1. Execute each step of the Aurelis installation script in sequence via SSH
2. After each step, capture stdout, stderr, and exit code
3. If a step succeeds (exit code 0), log the output and proceed to the next step
4. If a step fails, enter the **LLM retry loop** (identical protocol to the Packages Agent, up to 5 total attempts)
5. After all steps complete successfully, run a post-installation verification suite (service status checks, health endpoint probes, version verification)
6. Report final deployment status: Complete or Failed with itemized failures

**LLM retry loop:** Identical to the Packages Agent retry protocol. The LLM receives the failed command, the full output, the OS context, and the complete history of attempts for this step. It returns a diagnosis and a specific remediation command. Each of the 5 attempts addresses the most recent error, not necessarily the original failure.

**Post-installation verification:**
- All Aurelis services are running (`systemctl is-active ...`)
- Aurelis health endpoint returns HTTP 200
- Installed version matches the expected release string
- No critical errors in the Aurelis service log in the 60 seconds following startup

**Outputs:**
- Installation result: success or failure with per-step status
- SQLite audit entries for every command, every LLM prompt, every LLM response, and the post-installation verification results
- If all steps succeed: a deployment success notification with the Aurelis endpoint URL
- If any step exhausts all retries: a user alert notification (see Section 7)

---

## 6. LLM Reasoning Layer

**Runtime:** Ollama (local, no internet dependency)  
**Supported models:** Qwen3 (preferred), Mistral 7B, Gemma 4  
**API:** All agents call the same local Ollama HTTP endpoint

### Prompt Engineering

Every LLM call in the retry loop uses a structured prompt with the following sections:

```
SYSTEM: You are a Linux system administration expert. Your task is to diagnose 
command failures and suggest specific shell commands to resolve them. 
Respond ONLY in JSON format.

CONTEXT:
- OS: {os_flavor} {os_version}
- Agent: {agent_name}
- Objective: {what we are trying to install or execute}

ATTEMPT HISTORY:
{for each past attempt:
  Attempt N:
    Command: <exact command>
    Exit code: <N>
    Output: <full stdout/stderr>
    LLM diagnosis: <what LLM said last time>
    LLM command: <what LLM suggested last time>
}

CURRENT FAILURE:
  Command: <exact command that just failed>
  Exit code: <N>
  Output: <full stdout/stderr>

TASK: Diagnose the root cause of the current failure. Suggest ONE shell command 
to resolve it. If the current error is different from the previous error, focus 
on the current error.

RESPOND IN THIS JSON FORMAT:
{
  "diagnosis": "...",
  "root_cause": "...",
  "suggested_command": "...",
  "reasoning": "...",
  "confidence": 0.0-1.0
}
```

### Model Selection Fallback
If the primary model (Qwen3) returns a malformed response or times out, the agent automatically falls back to the next model in the list. If all models fail, the agent marks the step as unrecoverable and proceeds as if all retries are exhausted.

---

## 7. Retry and Fail-Safe Behavior

### Retry Budget

| Agent | Max retries per item | Retry scope |
|---|---|---|
| Requirements Extraction | 3 | Per LLM extraction call (JSON parse failures) |
| Inventory | 3 | Per SSH command (non-zero exit) |
| Packages | 5 | Per package installation, error-chain aware |
| Installer | 5 | Per installation step, error-chain aware |

### Error-Chain Awareness

The retry loop is not a simple "try the same thing again" mechanism. Each attempt:
1. Reads the error output of the previous attempt
2. Passes the full attempt history to the LLM (including what was tried and what happened)
3. Asks the LLM to address the most recent error, not the original one
4. Executes the LLM-suggested command

This means the retry sequence might look like:

```
Attempt 1: apt-get install docker-ce           → FAIL: GPG key not found
Attempt 2: curl .../gpg | gpg --dearmor ...    → SUCCESS (GPG key added)
           apt-get install docker-ce           → FAIL: dependency libseccomp2
Attempt 3: apt-get install libseccomp2         → SUCCESS
           apt-get install docker-ce           → FAIL: package not in repo
Attempt 4: add-apt-repository docker-stable    → SUCCESS
           apt-get install docker-ce           → SUCCESS
Attempt 5: (not needed)
```

### Exhaustion Behavior

When all retry attempts for a step are exhausted:
- The step is marked as FAILED in the audit log
- All attempt history (commands, outputs, LLM responses) is preserved in full
- The agent does NOT abort immediately — it continues to other items if applicable
- For the Packages Agent: execution continues to the next package; Installer Agent will not start if critical packages remain failed
- For the Installer Agent: execution halts and user notification is triggered

### User Notification (Retry Exhaustion)

When the Installer Agent (or Packages Agent, for critical items) exhausts retries:
- A notification is generated containing:
  - Which step failed
  - All 5 attempted commands and their outputs
  - All LLM diagnoses and suggestions
  - The most recent error in plain language
  - Suggested manual next steps (from the LLM's final response)
- The notification is surfaced in the Streamlit UI (alert banner) and written to the session report
- The user can choose to: manually intervene and rerun from the failed step, skip the step, or abort the deployment

---

## 8. Audit Logging and Session Reports

### SQLite Schema

**`sessions` table:**
```sql
CREATE TABLE sessions (
  id          TEXT PRIMARY KEY,
  started_at  TIMESTAMP,
  ended_at    TIMESTAMP,
  target_host TEXT,
  doc_version TEXT,
  status      TEXT  -- running / complete / failed
);
```

**`events` table:**
```sql
CREATE TABLE events (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id   TEXT REFERENCES sessions(id),
  timestamp    TIMESTAMP,
  agent        TEXT,    -- req_extraction / inventory / packages / installer
  event_type   TEXT,    -- command / llm_prompt / llm_response / result
  item         TEXT,    -- e.g. "docker", "ram_gb", "aurelis_step_3"
  attempt_num  INTEGER,
  content      TEXT,    -- full command string, prompt text, or response JSON
  exit_code    INTEGER, -- for command events
  status       TEXT     -- success / fail / retry / exhausted
);
```

### Session Report

A session report is generated at the end of each run (regardless of outcome). It contains:

- Run summary: target host, documentation version, start/end time, overall status
- Requirements summary: what was extracted, what was auto-generated
- Inventory summary: full comparison table with Met/Missing/Insufficient for each item
- Installation log: for each package, the sequence of commands run and outcomes
- Installer log: for each installation step, the sequence of commands run and outcomes
- Failure detail: for any exhausted retry chains, the full LLM conversation history
- Searchable: all text fields in the `events` table are indexed; the Streamlit UI exposes a search interface to filter by agent, item, status, and timestamp range

---

## 9. Infrastructure and Tools

| Component | Technology | Notes |
|---|---|---|
| Agent logic | Python 3.10+ | All agents, orchestration, SSH, LLM calls |
| LLM runtime | Ollama (local) | Runs on dedicated LLM server |
| LLM models | Qwen3, Mistral 7B, Gemma 4 | Model selection is configurable |
| SSH connectivity | Paramiko | Key-based and password auth supported |
| Vector database | ChromaDB or FAISS | For RAG in Requirements Extraction Agent |
| Embeddings | sentence-transformers | `all-MiniLM-L6-v2` or equivalent |
| Skills library | YAML files | Extended by Requirements Agent for new packages |
| Audit database | SQLite | Single file, portable, no server required |
| UI | Streamlit | Browser-based, tabs per agent |
| Target server OS | Ubuntu 20.04+, RHEL 8+, CentOS 8+ | OS flavor detected at runtime |

---

## 10. User Interface (Streamlit)

The Streamlit UI provides a single-page application with the following tabs:

**Tab 1 — Configuration**
- SSH credentials (host, port, username, key file or password)
- LLM server endpoint and model selection
- Documentation source (file upload or directory path)
- Run controls: Start pipeline, Stop, Rerun from step

**Tab 2 — Requirements**
- Parsed requirements JSON displayed as a table
- Expandable view of raw LLM extraction calls
- YAML skills auto-generated in this run

**Tab 3 — Inventory**
- Full comparison table: Required vs Found vs Status
- Color coding: green (Met), yellow (Insufficient), red (Missing)
- Raw SSH command log

**Tab 4 — Packages**
- Per-package installation status with progress indicators
- Expandable retry history for any failed package
- LLM diagnosis text for each retry attempt

**Tab 5 — Installer**
- Step-by-step script execution progress
- Live output streaming from SSH
- Expandable retry history and LLM conversations

**Tab 6 — Logs**
- Full session event log with filters by agent, item, status, and time range
- Export to CSV or JSON
- Session report download (PDF or Markdown)

---

## 11. Security Considerations

- SSH credentials are never written to disk or logged in the audit database; they are held in memory for the session duration only
- The Ollama LLM runs locally — no data leaves the internal network
- All SSH commands executed by the agents are logged in full; there is no silent command execution
- The YAML skills library is the only source of install commands used by the Packages Agent; it cannot be modified by LLM responses (LLM may only suggest commands, a human must accept new YAML skills in production)
- In a hardened deployment, the Packages and Installer agents should run with a dedicated SSH user that has only the minimum sudo privileges required for Aurelis installation

---

## 12. Acceptance Criteria

| # | Criterion |
|---|---|
| 1 | Requirements Extraction Agent correctly identifies all hardware and software prerequisites from the Nokia Aurelis R25.12 documentation |
| 2 | Inventory Agent connects to a Linux server via SSH and produces a complete Met/Missing/Insufficient table |
| 3 | Packages Agent successfully installs all Missing/Insufficient items on a clean Ubuntu 22.04 VM |
| 4 | Packages Agent correctly handles the Docker, Python, Helm, and Kubernetes installation cases |
| 5 | LLM retry loop resolves at least one multi-step error chain without human intervention |
| 6 | All 5 retry attempts are exhausted for a deliberately broken package, and the user is notified with the full LLM conversation history |
| 7 | Installer Agent runs the Aurelis installation script to completion on a fully prepared server |
| 8 | The session report contains enough information to reconstruct every decision made during the run |
| 9 | The Streamlit UI surfaces real-time progress across all four agents |
| 10 | The entire pipeline (clean server to running Aurelis) completes without human intervention on a server that meets minimum requirements |

---

## 13. References

1. Yao, S. et al. (2023). "ReAct: Synergizing Reasoning and Acting in Language Models." ICLR 2023.
2. Wei, J. et al. (2022). "Chain-of-Thought Prompting Elicits Reasoning in Large Language Models." NeurIPS 2022.
3. Wang, L. et al. (2024). "A Survey on Large Language Model based Autonomous Agents." Frontiers of Computer Science.
4. Ollama Documentation — https://ollama.com/
5. Streamlit Documentation — https://docs.streamlit.io/
6. Paramiko Documentation — https://www.paramiko.org/
7. PyYAML Documentation — https://pyyaml.org/
8. Nokia Aurelis Command Center Installation Guide, Release R25.12

---

*This document was prepared by the VIT Chennai SENSE team as part of the Nokia industry collaboration on AI-driven telecom infrastructure automation.*
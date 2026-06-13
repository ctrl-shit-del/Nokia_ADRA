# Product Requirements Document — v2
## ADRA — Autonomous Deployment Readiness Agent
### Nokia Aurelis Command Center · Fleet-Ready, UI-Driven Architecture

---

**Version:** 2.0
**Date:** June 2026
**Status:** Draft — Reflects Nokia review feedback
**Supersedes:** PRD v1.0 (May 2026)
**Repo:** github.com/ctrl-shit-del/Nokia_ADRA

---

## 1. What Changed Since v1

Nokia reviewed the v1 pipeline (Requirements → Inventory → Packages → Installer, each a standalone CLI script with an Ollama backend) and asked for the following before considering it production-track:

| Area | v1 | v2 |
|---|---|---|
| Execution model | 4 standalone CLI scripts | Same 4 independent agent modules, now invoked individually from a web UI — no agent depends on another running in-process |
| Requirements input | Always parse a local `docs/` folder | UI offers: upload PDF/HTML, pick a known version from a dropdown (DB-backed), or define+save a custom version |
| Requirements reuse | None — re-extracted every run | Versioned requirement profiles stored in a database; dropdown selections skip RAG/LLM entirely |
| Security | None | LLM flags potentially malicious software/commands found in the requirements doc; flag travels through inventory → packages UI |
| Packages UI | None (CLI only) | Checklist of packages to install, pre-checked from the gap table, with malicious-flag indicators |
| Skill storage | Python dict in `packages_agent.py` | `skills/` directory of YAML files, one per package; agent reads/writes these at runtime |
| Fleet learning | None | A fix the LLM discovers on server #1 (e.g. `python` → `python3` on Ubuntu) is persisted to the skill file so servers #2–#1000 use the corrected command on the first try |
| Reporting | Per-run console + SQLite | Fleet-wide session report: per-server status, drill-down logs with timestamps, exportable |
| LLM backend | Ollama (`gemma4:31b-cloud`, etc.) | llama.cpp (`llama-server`, OpenAI-compatible endpoint) |
| Token visibility | None | Live token-usage bar in the UI footer, updated per LLM call |
| UI | None | Web UI (FastAPI backend + React frontend), light/clean design |

---

## 2. Architecture Overview

### 2.1 Independence principle

Each agent remains a **self-contained Python module** with its own `run()` entry point and its own CLI (`python -m agents.requirements_agent`). The UI does not merge agents into one process — it calls each agent's `run()` function (or subprocess) independently, passing a small JSON context object and reading back a JSON result. This means:

- A user can still run any agent from the terminal exactly as in v1
- The UI is a thin orchestration + presentation layer, not a rewrite of agent logic
- Agents communicate only through files (`requirements.json`, `inventory.json`, `session_report.md`) and the shared SQLite audit DB — never through shared in-memory state

```
┌──────────────────────────────────────────────────────────────┐
│                         Web UI (React)                        │
│  ┌────────────┐ ┌────────────┐ ┌────────────┐ ┌────────────┐  │
│  │Requirements│ │ Inventory  │ │  Packages  │ │  Installer │  │
│  │    Tab     │ │    Tab     │ │    Tab     │ │    Tab     │  │
│  └─────┬──────┘ └─────┬──────┘ └─────┬──────┘ └─────┬──────┘  │
│        │              │              │              │         │
│  ──────┴──────────────┴──────────────┴──────────────┴──────   │
│                    Token usage bar (live, footer)              │
└────────┼──────────────┼──────────────┼──────────────┼─────────┘
         │              │              │              │
   ┌─────▼─────┐  ┌─────▼─────┐  ┌─────▼─────┐  ┌─────▼─────┐
   │Requirements│  │ Inventory │  │ Packages  │  │ Installer │
   │  Agent     │  │  Agent    │  │  Agent    │  │  Agent    │
   │(standalone)│  │(standalone)│ │(standalone)│ │(standalone)│
   └─────┬──────┘  └─────┬─────┘  └─────┬─────┘  └─────┬─────┘
         │               │              │              │
         ▼               ▼              ▼              ▼
  requirement_      inventory.json  skills/*.yaml  session_report.md
  profiles.db       + session.md    adra_audit.db  adra_audit.db
  (version DB)

         LLM calls from all agents → llama-server (OpenAI-compatible API)
                       → token usage streamed to UI footer
```

### 2.2 FastAPI backend, agent contract

Every agent exposes:

```python
def run(context: dict) -> dict:
    """
    context: small JSON object describing what this invocation needs
             (e.g. {"mode": "dropdown", "version": "xyz26"})
    returns: {"status": "ok"|"error", "result_path": "...", "summary": {...},
              "tokens_used": {"prompt": N, "completion": N}}
    """
```

FastAPI wraps each `run()` in an endpoint (`POST /api/requirements/run`, `POST /api/inventory/run`, etc.), runs it in a background task, and streams progress + token counts to the UI over Server-Sent Events (SSE) or WebSocket.

---

## 3. Requirements Agent v2

### 3.1 Three input modes (UI tab)

**Mode A — Known version (dropdown)**
User picks a version (e.g. `xyz26`, `xyz25`, `xyz24`, `xyz20`) from a dropdown populated from the `requirement_profiles` table. The agent:
1. Loads the stored requirements JSON directly from the DB
2. **Does not call the LLM or vector DB at all** — this is the token-saving path
3. Writes `requirements.json` and tags it `source: "profile:xyz26"`

**Mode B — Upload document (new/custom version)**
User uploads a PDF or HTML manual (150–250 pages typical). The agent:
1. Chunks and embeds the document into the vector DB (per-session collection, not merged into the global one until saved)
2. Runs the same multi-query RAG extraction as v1 (CPU, RAM, disk, OS, Python, Docker, Kubernetes, Helm, plus any product-specific items found)
3. Additionally runs a **security scan pass** (Section 3.3)
4. Writes `requirements.json` tagged `source: "upload:<filename>:<sha256[:8]>"`

**Mode C — Custom version, save for reuse**
Same as Mode B, but after extraction the UI asks:

> "Save this as a reusable version profile?"  [Save] [Don't save]

If saved, the user names it (e.g. `xyz26.5-custom-edge`) and the agent writes a row to `requirement_profiles` containing the version name, the full requirements JSON, the source document hash, and a timestamp. It then appears in the Mode A dropdown for all future runs — on this machine or any other ADRA instance pointed at the same DB.

### 3.2 Database schema

```sql
CREATE TABLE requirement_profiles (
    version_name   TEXT PRIMARY KEY,    -- e.g. "xyz26", "xyz26.5-custom-edge"
    requirements   TEXT,                -- full requirements.json as text
    source_type    TEXT,                -- "official" | "custom"
    source_doc_sha TEXT,                -- sha256 of the uploaded doc, null for official
    malicious_flags TEXT,               -- JSON array, see 3.3
    created_at     TEXT,
    created_by     TEXT                 -- username, for audit
);
```

Pre-seed `xyz20`, `xyz24`, `xyz25`, `xyz26` as `source_type="official"` from Nokia's published documentation during initial setup.

### 3.3 Malicious software detection

After extraction, in Mode B/C, a second LLM pass scans the extracted requirements **and** any shell commands/package names mentioned in the source document for red flags:

- Packages from non-standard or known-malicious repositories
- Commands that disable security features (`setenforce 0`, firewall flushes, etc.) presented as "requirements"
- Binaries fetched from suspicious URLs (raw IPs, pastebin-style hosts, non-HTTPS)

```json
"malicious_flags": [
  {
    "item": "custom-agent-binary",
    "reason": "Installation command downloads an unsigned binary from a raw IP address over HTTP",
    "severity": "high",
    "source_excerpt_ref": "doc chunk #47"
  }
]
```

This array is written into `requirements.json` and `requirement_profiles.malicious_flags`. It is **not acted on automatically** — it's surfaced to the user in the Packages tab (Section 5).

---

## 4. Inventory Agent v2

### 4.1 Session context — no redundant DB/LLM calls

The UI passes the Requirements Agent's output context forward:

```json
{
  "version_selected": "xyz26",
  "source": "profile" | "upload" | "custom",
  "requirements_path": "requirements.json",
  "malicious_flags": [...]
}
```

If `source == "profile"`, the Inventory Agent already has everything it needs from `requirements.json` — it goes straight to the SSH planner loop with **zero additional vector DB queries**. The "why call the LLM with DB content again" point from the brief is handled here: the requirements were already resolved once during Mode A's DB lookup, so inventory only needs the planner loop (LLM calls to decide *which SSH commands to run*, which v1 already does efficiently).

### 4.2 Behavior — otherwise unchanged from v1

Same autonomous planner loop, same `extract_json`, same exit guard, same Reflexion-style memory (Section 7, v1 PRD). Output is `inventory.json` plus a `session_report.md` (per-server) that downstream agents and the UI both read.

`malicious_flags` from `requirements.json` are passed through unchanged into `inventory.json` so the Packages tab can render them without re-deriving anything.

---

## 5. Packages Agent v2

### 5.1 UI — pre-flight checklist

Before any installation starts, the Packages tab renders a table built from `inventory.json`'s `Missing`/`Insufficient` items:

```
┌─────────────────────────────────────────────────────────────┐
│  Packages to install                                          │
│                                                                 │
│  ☑  docker        20.10 → not found                            │
│  ☑  kubernetes    1.26  → not found                            │
│  ☑  helm          3.10  → not found                            │
│  ☑  custom-agent  2.1   → not found   ⚠ Flagged — see below   │
│                                                                 │
│  ⚠ custom-agent: installation command downloads an unsigned   │
│    binary from a raw IP over HTTP. Reviewed by: ___________   │
│                                                                 │
│  Hardware: ram_gb insufficient (14/32) — installs will proceed │
│  but Aurelis may not run correctly. [Acknowledge]              │
│                                                                 │
│              [ Start Installation ]                            │
└─────────────────────────────────────────────────────────────┘
```

All checkboxes default to **checked**. Unchecking a package excludes it from this run (e.g. user wants to install it manually, or it's flagged and they want to review first). Flagged items show the LLM's reason inline and require an explicit acknowledgment click before "Start Installation" is enabled — this satisfies "show the user... probably there will be like something in front of that software that it is malicious found by the llm" while not silently blocking the run.

### 5.2 Skill directory (filesystem-based)

Replace the in-script `SKILLS` dict with a directory:

```
skills/
  python.yaml
  docker.yaml
  kubernetes.yaml
  helm.yaml
  custom-agent.yaml      ← auto-created if not present
```

Each file:

```yaml
# skills/python.yaml
description: "Python 3 interpreter"
check_cmd: "python3 --version 2>&1"
install:
  ubuntu:
    - "sudo apt-get update -y"
    - "sudo apt-get install -y python3 python3-pip python3-venv"
  rhel:
    - "sudo yum install -y python3 python3-pip"
verify_cmd: "python3 --version 2>&1"

# Auto-appended by the agent after a successful LLM-guided fix:
learned_fixes:
  - matched_error: "Unable to locate package python"
    os: "ubuntu"
    original_command: "sudo apt-get install -y python"
    fixed_command: "sudo apt-get install -y python3 python3-pip python3-venv"
    learned_at: "2026-06-14T10:32:00"
    confidence: 0.9
```

### 5.3 Self-healing across the fleet

This is the core of the "1000 servers" point in the brief. The retry flow becomes:

1. Before running an install step, check `learned_fixes` in the skill file for an entry whose `matched_error` is a substring/regex match against... nothing yet (first run, no error). Run the normal `install` command.
2. If it fails, check `learned_fixes` **first** — if a learned fix exists for this OS and the error text matches `matched_error`, apply the `fixed_command` directly. **No LLM call.**
3. If no learned fix matches, fall back to the LLM retry loop exactly as in v1 (max 5 attempts, error-chain aware).
4. If the LLM resolves it, append a new entry to `learned_fixes` in the skill YAML — `matched_error` is a normalized substring of the error output, `fixed_command` is what worked.
5. On the *next* server in the fleet run, step 2 catches the same error and resolves it with zero LLM calls.

```
Server 1:  install fails → no learned_fixes → LLM (5 attempts) → fix found
                          → write learned_fixes entry to docker.yaml
Server 2:  install fails → learned_fixes matches → apply fixed_command directly
           (0 LLM calls, ~instant)
Server 3-1000: same as Server 2
```

`learned_fixes` entries are append-only and human-reviewable — a YAML diff in the skill repo is itself an audit trail of what ADRA has learned.

### 5.4 Two-pass install order

Per the brief: "during installing... if not downloaded it should be proceeding to install the next components and at last once again try the failed packages."

```
Pass 1: for each checked package → install (with learned-fix / LLM retry as above)
        → collect failures into retry_queue

Pass 2: for each package in retry_queue → one more full attempt
        (learned_fixes + LLM retry again — useful if Pass 1's failure
         was caused by a dependency that Pass 1 installed later)

→ anything still failing after Pass 2 is reported as failed in the session report
```

---

## 6. Installer Agent v2

Functionally unchanged from v1 (sequential Aurelis script execution, LLM retry loop, hard hardware gate, post-install verification). Two additions:

- Reads `learned_fixes` from `skills/` the same way Packages Agent does, for any installer-step failures that resemble package-install errors (e.g. a missing CLI tool discovered mid-script)
- Writes its portion of the session report using the same per-server schema as Packages Agent, so the fleet dashboard (Section 7) can merge both

---

## 7. Fleet Session Report & UI

### 7.1 Per-server record

```json
{
  "server": "10.0.1.23",
  "version": "xyz26",
  "status": "success" | "partial" | "failed",
  "packages": {
    "docker": "success",
    "kubernetes": "success",
    "helm": "failed"
  },
  "installer": "not_started" | "in_progress" | "success" | "failed",
  "malicious_flags_acknowledged": ["custom-agent"],
  "timestamps": {"started": "...", "ended": "..."},
  "report_path": "reports/10.0.1.23_20260614.md"
}
```

### 7.2 Fleet dashboard view

```
┌────────────────────────────────────────────────────────────┐
│  Fleet Summary — xyz26 rollout                                │
│                                                                │
│  ✓ 847 servers — fully installed                              │
│  ⚠  112 servers — partial (helm failed, see logs)             │
│  ✗  41 servers  — failed (hardware insufficient)              │
│                                                                │
│  [Filter: Failed ▾]   [Export CSV]                            │
│                                                                │
│  10.0.1.23   ✗ helm failed   2026-06-14 10:32   [View logs]  │
│  10.0.1.41   ✗ helm failed   2026-06-14 10:35   [View logs]  │
└────────────────────────────────────────────────────────────┘
```

Clicking "View logs" opens the per-server `session_report.md` (same markdown report format from v1, Section 11) with full LLM retry history and timestamps.

---

## 8. LLM Backend — llama.cpp

Replace all `requests.post(OLLAMA_URL, ...)` calls with calls to `llama-server`'s OpenAI-compatible endpoint:

```python
LLAMACPP_URL = "http://localhost:8080/v1/chat/completions"

def call_llm(prompt: str) -> tuple[str, dict]:
    payload = {
        "model": "local",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
    }
    resp = requests.post(LLAMACPP_URL, json=payload, timeout=180)
    resp.raise_for_status()
    data = resp.json()
    text = data["choices"][0]["message"]["content"]
    usage = data.get("usage", {"prompt_tokens": 0, "completion_tokens": 0})
    return text, usage
```

Every `call_llm` call returns `(text, usage)`. Agents accumulate `usage` into a running session total and report it back to the UI in the `run()` return value (`tokens_used`). The UI footer subscribes to this via SSE and updates live.

Run `llama-server` with a quantized GGUF model sized to fit the available RAM (e.g. Qwen2.5-7B-Instruct Q4_K_M fits comfortably in 8GB and is a good fit given the earlier RAM constraints discussed).

---

## 9. UI/UX Requirements

- **Framework:** FastAPI backend, React + Tailwind frontend (see companion UI design prompt for visual direction)
- **Tabs:** Requirements / Inventory / Packages / Installer / Fleet Report — each tab is independently runnable; running one does not require the others to have run in this session (though logically Inventory needs `requirements.json` to exist on disk)
- **Token usage bar:** fixed footer, shows running total for the current session (`prompt: X · completion: Y · total: Z`), updates after every LLM call via SSE
- **Light, clean design:** per Nokia's "go in real direction" framing, this should look like an internal ops tool — calm, readable, data-dense where needed (gap tables, fleet dashboard) but uncluttered
- **Malicious flag styling:** distinct but not alarmist — amber/warning tone, not red/blocking, since the user retains judgment

---

## 10. Acceptance Criteria (v2 additions)

| # | Criterion |
|---|---|
| 1 | Each agent can still be run standalone via `python -m agents.<name>` with identical behavior to the UI-driven run |
| 2 | Selecting a known version from the dropdown produces `requirements.json` with zero LLM/vector-DB calls |
| 3 | Uploading a custom 150–250 page PDF completes extraction and offers to save as a named profile |
| 4 | A saved custom profile appears in the dropdown on a subsequent run, including from a different ADRA instance pointed at the same DB |
| 5 | A deliberately malicious-looking install command in a test doc is flagged in the Packages tab and requires acknowledgment before proceeding |
| 6 | A package install failure resolved by the LLM on server 1 is resolved via `learned_fixes` (zero LLM calls) on server 2 with the same error |
| 7 | The two-pass install order correctly retries Pass-1 failures in Pass 2 |
| 8 | The token usage footer updates in real time during a Requirements Agent RAG extraction run |
| 9 | All LLM calls route through llama-server; no Ollama dependency remains |
| 10 | The fleet dashboard correctly aggregates per-server status across at least 3 concurrent test servers |

---

## 11. Open Questions for Nokia

- What is the canonical source for `xyz20`/`xyz24`/`xyz25`/`xyz26` requirement profiles — should ADRA ship pre-seeded, or pull from Nokia's internal documentation portal on first run?
- Is the `requirement_profiles` DB shared across an org (central server) or per-ADRA-instance (local SQLite)? This affects whether Mode C's "save for reuse" is local-only or fleet-wide on day one.
- What GGUF model + quantization does Nokia's internal llama.cpp deployment standardize on? This affects prompt sizing and `MAX_RETRIES` tuning.
- For the malicious-software scan — should flagged items ever hard-block (no acknowledgment override), e.g. for a denylist of known-bad signatures?

---

*This document supersedes PRD v1.0 and was prepared by the VIT Chennai SENSE team following Nokia's review of the initial pipeline demo.*
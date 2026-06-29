import json
import re
import sqlite3
import paramiko
import getpass
import requests
import datetime
import time
from pathlib import Path
from adra_common import TokenCounter, call_llm as call_llamacpp, set_model_override
from packages_agent import append_learned_fix, find_learned_fix, load_skills, normalize_error_match

# ==========================================
# 1. Configuration
# ==========================================
SSH_HOST = "127.0.0.1"
SSH_PORT = 22
SSH_USER = "mystic"

# Same flag as packages_agent — set True only if sudoers has NOPASSWD
PASSWORDLESS_SUDO = False

MAX_RETRIES = 5
DB_PATH     = "adra_audit.db"
REPORT_PATH = "session_report.md"
TOKENS      = TokenCounter()

# How long to wait (seconds) after installation before running health checks
POST_INSTALL_SETTLE_TIME = 30

# ==========================================
# 2. Autonomous Planner
# ==========================================
def generate_dynamic_steps(system_name: str, os_flavor: str, source: str) -> list[dict]:
    print(f"  [Planner] No cached installation steps found for '{system_name}'.")
    print(f"  [Planner] Querying LLM to generate installation pipeline...")
    
    prompt = f"""You are an expert Linux sysadmin designing an installation pipeline.
We need to install '{system_name}' on an '{os_flavor}' system.

"""
    if source.startswith("upload:"):
        parts = source.split(":")
        if len(parts) >= 2:
            filename = parts[1]
            docs_path = Path("docs") / filename
            if docs_path.exists():
                try:
                    manual_text = docs_path.read_text(encoding="utf-8")
                    prompt += f"The user has provided the following installation manual/documentation:\\n---\\n{manual_text[:6000]}\\n---\\n\\n"
                except Exception:
                    pass

    prompt += """Please provide the installation steps as a JSON list of objects.
Each object must have exactly these keys:
- "name": human-readable label
- "command": the exact bash command to execute
- "sudo": boolean (true if it needs root privileges)
- "critical": boolean (true if failure should abort the installation)
- "verify": a bash command to verify success, or null

CRITICAL RULE 1: Do NOT place `sudo` on the right side of a pipe (e.g. avoid `curl | sudo bash` or `echo | sudo tee`). Instead, wrap the entire command or the pipe sequence in a sudo bash session (e.g. `sudo bash -c 'curl | bash'`), or write to files safely without pipes.
CRITICAL RULE 2: If you need to set environment variables or source a script (e.g. `source /opt/ros/humble/setup.bash`), you MUST append it to ~/.bashrc using `echo \"source ...\" >> ~/.bashrc` instead of just running it. The command will only persist if you write it to ~/.bashrc.

Output ONLY a raw JSON array. No markdown fences, no explanation.
[
  {
    "name": "Update Apt",
    "command": "apt-get update",
    "sudo": true,
    "critical": true,
    "verify": null
  }
]
"""
    raw = call_llm(prompt)
    if not raw:
        return []
    
    try:
        text = re.sub(r"```(?:json)?\s*", "", raw)
        text = re.sub(r"```", "", text).strip()
        # Remove <think>...</think> blocks
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
        match = re.search(r"\[.*\]", text, re.DOTALL)
        json_text = match.group() if match else text
        # Fix invalid backslash escapes that LLMs commonly produce
        json_text = re.sub(r'\\(?!["\\bfnrtu/])', r'\\\\', json_text)
        return json.loads(json_text)
    except Exception as e:
        print(f"  [Planner] Failed to parse generated steps: {e}")
        print(f"  [Planner] Retrying LLM generation...")
        # Retry once with a stricter prompt
        raw2 = call_llm(prompt + "\nIMPORTANT: Your previous response had a JSON parse error. Ensure all backslashes in commands are properly escaped as double backslashes. Output ONLY the raw JSON array.")
        if raw2:
            try:
                text2 = re.sub(r"```(?:json)?\s*", "", raw2)
                text2 = re.sub(r"```", "", text2).strip()
                text2 = re.sub(r"<think>.*?</think>", "", text2, flags=re.DOTALL).strip()
                match2 = re.search(r"\[.*\]", text2, re.DOTALL)
                json_text2 = match2.group() if match2 else text2
                json_text2 = re.sub(r'\\(?!["\\bfnrtu/])', r'\\\\', json_text2)
                return json.loads(json_text2)
            except Exception as e2:
                print(f"  [Planner] Retry also failed: {e2}")
        return []


# ==========================================
# 4. JSON Parsing
# ==========================================
def extract_json(raw: str) -> dict:
    if not raw:
        raise ValueError("Empty LLM response")
    text = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    text = re.sub(r"```(?:json)?\s*", "", text)
    text = re.sub(r"```", "", text).strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"No JSON object found:\n{raw[:300]}")
    return json.loads(match.group())


# ==========================================
# 5. SSH Execution
# ==========================================
def execute_ssh(
    client: paramiko.SSHClient,
    command: str,
    sudo_password: str | None = None,
) -> tuple[int, str]:
    """
    Run a command over SSH.
    Commands starting with 'sudo' use sudo -S (reads password from stdin).
    PASSWORDLESS_SUDO=True skips the stdin write.
    """
    display = command[:100] + ("..." if len(command) > 100 else "")
    print(f"  [SSH] {display}")

    if "sudo " in command and not PASSWORDLESS_SUDO:
        sudo_cmd = command.replace("sudo ", "sudo -S -p '' ")
        stdin, stdout, stderr = client.exec_command(sudo_cmd)
        if sudo_password:
            for _ in range(command.count("sudo ")):
                stdin.write(sudo_password + "\n")
            stdin.flush()
            stdin.channel.shutdown_write()
    else:
        stdin, stdout, stderr = client.exec_command(command)

    exit_code = stdout.channel.recv_exit_status()
    out = stdout.read().decode("utf-8", errors="replace").strip()
    err = stderr.read().decode("utf-8", errors="replace").strip()
    combined = "\n".join(filter(None, [out, err]))

    if exit_code != 0:
        print(f"  [Exit {exit_code}] {combined[:300]}")
    else:
        print(f"  [OK] {combined[:120]}")

    return exit_code, combined


# ==========================================
# 6. LLM
# ==========================================
def call_llm(prompt: str) -> str | None:
    text, _ = call_llamacpp(prompt, TOKENS, timeout=900)
    return text


# ==========================================
# 7. SQLite Audit Logging
# ==========================================
def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            id          TEXT PRIMARY KEY,
            started_at  TEXT,
            ended_at    TEXT,
            target_host TEXT,
            status      TEXT
        );
        CREATE TABLE IF NOT EXISTS events (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id   TEXT,
            timestamp    TEXT,
            agent        TEXT,
            event_type   TEXT,
            item         TEXT,
            attempt_num  INTEGER,
            content      TEXT,
            exit_code    INTEGER,
            status       TEXT
        );
    """)
    conn.commit()
    return conn


def log_event(conn, session_id, agent, event_type, item,
              attempt_num, content, exit_code, status):
    conn.execute(
        "INSERT INTO events VALUES (NULL,?,?,?,?,?,?,?,?,?)",
        (session_id, datetime.datetime.now().isoformat(),
         agent, event_type, item, attempt_num,
         str(content)[:4000], exit_code, status),
    )
    conn.commit()


def open_session(conn, session_id, host):
    conn.execute(
        "INSERT INTO sessions VALUES (?,?,NULL,?,?)",
        (session_id, datetime.datetime.now().isoformat(), host, "running"),
    )
    conn.commit()


def close_session(conn, session_id, status):
    conn.execute(
        "UPDATE sessions SET ended_at=?, status=? WHERE id=?",
        (datetime.datetime.now().isoformat(), status, session_id),
    )
    conn.commit()


# ==========================================
# 8. LLM Retry Loop
# ==========================================
def ask_llm_for_fix(
    step_name: str,
    os_flavor: str,
    attempt_history: list[dict],
) -> dict | None:
    """
    Given the attempt history for one failing installation step,
    ask the LLM to diagnose the current error and suggest ONE command.
    """
    history_lines = []
    for entry in attempt_history:
        history_lines.append(f"\nAttempt {entry['attempt']}:")
        history_lines.append(f"  Command:   {entry['command']}")
        history_lines.append(f"  Exit code: {entry['exit_code']}")
        history_lines.append(f"  Output:    {entry['output'][:600]}")
        if entry.get("llm_diagnosis"):
            history_lines.append(f"  LLM said:  {entry['llm_diagnosis']}")
        if entry.get("llm_suggested"):
            history_lines.append(f"  LLM tried: {entry['llm_suggested']}")

    current = attempt_history[-1]

    prompt = f"""You are a Nokia Aurelis Command Center deployment expert.
An automated installation script failed on one step. Diagnose the error and suggest ONE shell command to resolve it.

CONTEXT:
  OS:            {os_flavor}
  Install step:  {step_name}
  Attempt:       {current['attempt']} of {MAX_RETRIES}
  Stack:         Docker + kubectl + Helm on a kind (Kubernetes-in-Docker) cluster

ATTEMPT HISTORY (most recent is the current failure):
{''.join(history_lines)}

RULES:
1. Focus on the MOST RECENT error — not the original failure.
2. Do NOT suggest a command already in the attempt history.
3. Suggest ONE concrete shell command. If cleanup is needed before retry, include it with &&.
4. AVOID putting 'sudo' on the right side of a pipe (e.g. 'curl | sudo bash' or 'echo | sudo tee'). Instead, use 'sudo bash -c "curl | bash"' or another pattern that does not pipe into sudo.
5. If you see an apt 'Conflicting values set for option Signed-By' error, the root cause is duplicate .list files in /etc/apt/sources.list.d/ pointing to the same repo. Suggest a command to delete the older/duplicate .list file.
6. If your fix is a prerequisite (like killing a process to release a lock, or fixing a GPG key), you MUST combine it with the ORIGINAL failed command using `&&` (e.g. `sudo fuser -k /var/lib/dpkg/lock-frontend && sudo apt-get install -y <package>`). Your suggested command completely replaces the failed attempt in the cache, so it must accomplish the original step's goal!
7. Respond with ONLY raw JSON — no markdown, no preamble.

JSON FORMAT:
{{
  "diagnosis":         "root cause of the current failure in one sentence",
  "suggested_command": "exact shell command to run",
  "reasoning":         "why this will fix the current error",
  "confidence":        0.0
}}"""

    raw = call_llm(prompt)
    if not raw:
        return None
    try:
        return extract_json(raw)
    except (ValueError, json.JSONDecodeError) as e:
        print(f"  [LLM Parse Error] {e}")
        return None


# ==========================================
# 9. Execute One Installation Step
# ==========================================
def execute_step(
    client: paramiko.SSHClient,
    step: dict,
    os_flavor: str,
    conn: sqlite3.Connection,
    session_id: str,
    sudo_password: str | None,
    step_num: int,
    total_steps: int,
    skills: dict[str, dict] | None = None,
    use_llm: bool = True,
) -> tuple[bool, list[dict]]:
    """
    Run one installation step with LLM-guided retry on failure.

    Returns:
        (success: bool, attempt_history: list[dict])
    """
    name    = step["name"]
    cmd     = step["command"]
    use_sudo = step.get("sudo", False)

    if use_sudo and not cmd.startswith("sudo "):
        cmd = "sudo " + cmd

    print(f"\n  ── Step {step_num}/{total_steps}: {name} ──")
    log_event(conn, session_id, "installer_agent", "step_start",
              name, 0, cmd, None, "running")

    exit_code, output = execute_ssh(client, cmd, sudo_password)
    log_event(conn, session_id, "installer_agent", "command",
              name, 1, cmd, exit_code,
              "success" if exit_code == 0 else "fail")

    attempt_history: list[dict] = [{
        "attempt":       1,
        "command":       cmd,
        "exit_code":     exit_code,
        "output":        output,
        "llm_diagnosis": None,
        "llm_suggested": None,
    }]

    if exit_code == 0:
        # Run optional step-level verify
        verify_cmd = step.get("verify")
        if verify_cmd:
            v_exit, v_out = execute_ssh(client, verify_cmd, sudo_password=None)
            print(f"  [Step Verify] {'OK' if v_exit == 0 else 'WARN'}: {v_out[:120]}")
            log_event(conn, session_id, "installer_agent", "step_verify",
                      name, 0, v_out, v_exit, "success" if v_exit == 0 else "warn")
        return True, attempt_history

    skills = skills or {}
    for pkg_name, skill in skills.items():
        learned_cmd = find_learned_fix(skill, os_flavor, output)
        if not learned_cmd:
            continue
        print(f"  [Learned Fix:{pkg_name}] Applying persisted fix: {learned_cmd}")
        lf_exit, lf_output = execute_ssh(client, learned_cmd, sudo_password)
        log_event(conn, session_id, "installer_agent", "learned_fix",
                  name, 2, learned_cmd, lf_exit,
                  "success" if lf_exit == 0 else "fail")
        attempt_history.append({
            "attempt": 2,
            "command": learned_cmd,
            "exit_code": lf_exit,
            "output": lf_output,
            "llm_diagnosis": f"matched learned_fixes entry from {pkg_name}.yaml",
            "llm_suggested": learned_cmd,
        })
        if lf_exit == 0:
            return True, attempt_history
        break

    # ── Step failed — LLM retry loop ─────────────────────────────────────
    if not use_llm:
        print(f"\n  ⚠  Step failed. LLM is disabled, aborting retry loop.")
        log_event(conn, session_id, "installer_agent", "step_result",
                  name, 2, "LLM disabled, aborting retry", -1, "exhausted")
        return False, attempt_history

    print(f"\n  ⚠  Step failed. Entering LLM retry loop (max {MAX_RETRIES} attempts)...")

    for attempt_num in range(2, MAX_RETRIES + 1):
        print(f"\n  [Attempt {attempt_num}/{MAX_RETRIES}] Consulting LLM...")

        llm_result = ask_llm_for_fix(name, os_flavor, attempt_history)

        if not llm_result:
            print(f"  [LLM] No response on attempt {attempt_num}.")
            log_event(conn, session_id, "installer_agent", "llm_error",
                      name, attempt_num, "No LLM response", None, "fail")
            attempt_history.append({
                "attempt": attempt_num, "command": "(no LLM response)",
                "exit_code": -1, "output": "", "llm_diagnosis": None, "llm_suggested": None,
            })
            continue

        diagnosis     = llm_result.get("diagnosis", "")
        suggested_cmd = llm_result.get("suggested_command", "").strip()
        confidence    = llm_result.get("confidence", 0.0)

        print(f"  [LLM Diagnosis] {diagnosis}")
        print(f"  [LLM Suggests]  {suggested_cmd}")
        print(f"  [Confidence]    {confidence:.2f}")

        log_event(conn, session_id, "installer_agent", "llm_response",
                  name, attempt_num, json.dumps(llm_result), None, "retry")

        if not suggested_cmd:
            print("  [LLM] Empty command. Skipping attempt.")
            continue

        attempt_history[-1]["llm_diagnosis"] = diagnosis
        attempt_history[-1]["llm_suggested"] = suggested_cmd

        exit_code, output = execute_ssh(client, suggested_cmd, sudo_password)
        log_event(conn, session_id, "installer_agent", "command",
                  name, attempt_num, suggested_cmd, exit_code,
                  "success" if exit_code == 0 else "fail")

        attempt_history.append({
            "attempt":       attempt_num,
            "command":       suggested_cmd,
            "exit_code":     exit_code,
            "output":        output,
            "llm_diagnosis": None,
            "llm_suggested": None,
        })

        if exit_code == 0:
            print(f"\n  ✓ Resolved on attempt {attempt_num}.")
            append_learned_fix(
                "installer",
                skills.get("installer", {"description": "Installer learned fixes"}),
                os_flavor,
                normalize_error_match(attempt_history[0]["output"]),
                attempt_history[0]["command"],
                suggested_cmd,
                float(confidence or 0.0),
            )
            log_event(conn, session_id, "installer_agent", "step_result",
                      name, attempt_num, "Resolved by LLM", exit_code, "success")
            return True, attempt_history
        else:
            print("  Still failing. Continuing retry loop...")

    # All retries exhausted
    log_event(conn, session_id, "installer_agent", "step_result",
              name, MAX_RETRIES, "All retries exhausted", -1, "exhausted")
    return False, attempt_history


# ==========================================
# 10. Post-Installation Verification Suite
# ==========================================

# ==========================================
# 11. Session Report
# ==========================================
def generate_report(
    session_id: str,
    inventory: dict,
    os_flavor: str,
    step_results: list[dict],
    start_time: datetime.datetime,
) -> str:
    """Build a markdown session report and save it to REPORT_PATH."""
    end_time = datetime.datetime.now()
    duration = str(end_time - start_time).split(".")[0]

    overall = all(r["success"] for r in step_results if r["step"].get("critical", True))
    status_str = "✅ SUCCESS" if overall else "❌ FAILED"

    lines = [
        f"# ADRA Session Report — {os_flavor.upper()} Installer",
        f"",
        f"| Field | Value |",
        f"|---|---|",
        f"| Session ID | `{session_id}` |",
        f"| Target host | `{SSH_HOST}` |",
        f"| OS | {os_flavor} |",
        f"| Started | {start_time.strftime('%Y-%m-%d %H:%M:%S')} |",
        f"| Duration | {duration} |",
        f"| Overall status | **{status_str}** |",
        f"",
        f"---",
        f"",
        f"## Hardware State",
        f"",
        f"| Item | Required | Found | Status |",
        f"|---|---|---|---|",
    ]

    for item in inventory.get("hardware", []):
        status_icon = "✓" if item["status"] == "Met" else "⚠" if item["status"] == "Insufficient" else "✗"
        lines.append(
            f"| {item['item']} | {item['required']} | {item.get('found', 'null')} | {status_icon} {item['status']} |"
        )

    lines += [
        f"",
        f"## Software State",
        f"",
        f"| Item | Required | Found | Status |",
        f"|---|---|---|---|",
    ]

    for item in inventory.get("software", []):
        status_icon = "✓" if item["status"] == "Met" else "⚠" if item["status"] == "Insufficient" else "✗"
        lines.append(
            f"| {item['item']} | {item['required']} | {item.get('found', 'null')} | {status_icon} {item['status']} |"
        )

    lines += [
        f"",
        f"---",
        f"",
        f"## Installation Steps",
        f"",
    ]

    for result in step_results:
        step    = result["step"]
        success = result["success"]
        history = result["history"]
        icon    = "✅" if success else "❌"
        critical_tag = " *(critical)*" if step.get("critical") else " *(non-critical)*"

        lines.append(f"### {icon} {step['name']}{critical_tag}")
        lines.append(f"")
        lines.append(f"**Attempts:** {len(history)}/{MAX_RETRIES}")
        lines.append(f"")

        for entry in history:
            lines.append(f"**Attempt {entry['attempt']}**")
            lines.append(f"```")
            lines.append(f"$ {entry['command']}")
            lines.append(f"exit: {entry['exit_code']}")
            lines.append(f"{entry['output'][:400]}")
            lines.append(f"```")
            if entry.get("llm_diagnosis"):
                lines.append(f"> 🤖 **LLM:** {entry['llm_diagnosis']}")
                lines.append(f"> Suggested: `{entry.get('llm_suggested', '')}`")
            lines.append(f"")

    lines += [
        f"---",
        f"",
        f"## Audit Log",
        f"",
        f"Full command and LLM conversation history stored in: `{DB_PATH}`",
        f"",
        f"```sql",
        f"-- Search failed steps:",
        f"SELECT item, attempt_num, content, exit_code",
        f"FROM events WHERE agent='installer_agent' AND status IN ('fail','exhausted')",
        f"ORDER BY id;",
        f"```",
        f"",
        f"---",
        f"*Generated by ADRA — Autonomous Deployment Readiness Agent*  ",
        f"*VIT Chennai × Nokia*",
    ]

    report = "\n".join(lines)
    Path(REPORT_PATH).write_text(report, encoding="utf-8")
    return report


# ==========================================
# 12. Pre-flight Gates
# ==========================================
def check_inventory_gates(inventory: dict) -> tuple[list[str], list[str]]:
    """
    Check if there are blocking issues.
    Returns (hw_issues, sw_issues).
    """
    hw_issues = []
    sw_issues = []

    # Hardware
    for item in inventory.get("hardware", []):
        if item["status"] in ("Missing", "Insufficient"):
            hw_issues.append(
                f"Hardware '{item['item']}': requires {item['required']}, "
                f"found {item.get('found', 'null')} [{item['status']}]"
            )

    # Software
    for item in inventory.get("software", []):
        if item["status"] in ("Missing", "Insufficient"):
            sw_issues.append(
                f"Software '{item['item']}': requires {item['required']}, "
                f"found {item.get('found', 'null')} [{item['status']}] — run packages_agent.py first"
            )

    return hw_issues, sw_issues


# ==========================================
# 13. Main
# ==========================================
def run(context: dict | None = None) -> dict:
    context = context or {}
    use_llm = context.get("use_llm", True)
    set_model_override(context.get("model"))
    TOKENS.prompt = 0
    TOKENS.completion = 0
    start_time = datetime.datetime.now()

    # ── Load inventory ────────────────────────────────────────────────────
    try:
        with open("inventory.json") as f:
            inventory = json.load(f)
    except FileNotFoundError:
        print("Error: inventory.json not found. Run inventory_agent.py first.")
        return {"status": "error", "error": "inventory.json not found", "tokens_used": TOKENS.as_dict()}

    # Detect OS
    os_flavor = "ubuntu"
    for item in inventory.get("software", []):
        if item["item"] == "os_name" and item.get("found"):
            name = item["found"].lower()
            if "ubuntu" in name or "debian" in name:
                os_flavor = "ubuntu"
            elif any(k in name for k in ("rhel", "red hat", "centos", "fedora")):
                os_flavor = "rhel"

    print(f"\nDetected OS flavor: {os_flavor}")

    # ── Hard pre-flight gate ──────────────────────────────────────────────
    hw_issues, sw_issues = check_inventory_gates(inventory)
    
    if hw_issues or sw_issues:
        print("\n" + "━" * 58)
        print("  ⚠️  PRE-FLIGHT ISSUES DETECTED")
        print("━" * 58)
        for issue in hw_issues:
            print(f"  ✗  {issue}")
        for issue in sw_issues:
            print(f"  ✗  {issue}")
        print("━" * 58)

        if sw_issues:
            print("\n  🚫  INSTALLATION BLOCKED: Software dependencies not met.")
            print("  Resolve the software issues above and re-run packages_agent.py")
            return {"status": "error", "error": "software dependencies not met", "tokens_used": TOKENS.as_dict()}
            
        # Only hardware issues
        answer = context.get("acknowledge_hardware")
        if answer is None:
            answer = input("\n  Continue with installation despite hardware issues? (y/N): ").strip().lower() == "y"
        if not answer:
            print("Aborting.")
            return {"status": "error", "error": "hardware requirements not acknowledged", "tokens_used": TOKENS.as_dict()}

    print("\n✓ Pre-flight checks passed. Starting installation.\n")

    # ── Credentials ───────────────────────────────────────────────────────
    host = context.get("host", SSH_HOST)
    port = int(context.get("port", SSH_PORT))
    username = context.get("username", SSH_USER)
    ssh_password = context.get("password")
    if ssh_password is None:
        ssh_password = getpass.getpass(f"Enter SSH password for {username}@{host}: ")
    sudo_password = None if PASSWORDLESS_SUDO else ssh_password

    # ── Setup ─────────────────────────────────────────────────────────────
    conn = init_db()
    session_id = f"inst_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}"
    open_session(conn, session_id, host)

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    step_results: list[dict] = []
    installation_ok = False
    active_steps = []

    try:
        reqs = json.loads(Path("requirements.json").read_text(encoding="utf-8"))
        source = reqs.get("source", "")
        if source is None:
            source = ""
            
        system_name = "Target Software"
        if "ns-3" in source.lower() or "ns3" in source.lower():
            system_name = "NS-3 Simulator"
        elif "ros2" in source.lower() or "ros" in source.lower():
            system_name = "ROS2 Humble"
        elif source.startswith("upload:"):
            system_name = source.split(":")[1]
        elif source.startswith("profile:"):
            system_name = source.split(":")[1]

        cache_path = Path("installer_cache") / f"{system_name.replace(' ', '_').lower()}.json"
        
        if cache_path.exists():
            print(f"  [Planner] Loaded cached installation steps for '{system_name}'.")
            active_steps = json.loads(cache_path.read_text(encoding="utf-8"))
        elif use_llm:
            active_steps = generate_dynamic_steps(system_name, os_flavor, source)
        else:
            print(f"  [Error] No cached installation steps found for '{system_name}' and LLM is disabled.")
            return {"status": "error", "error": "No cached installation steps found for the target system and LLM generation is disabled.", "tokens_used": TOKENS.as_dict()}
            
        if not active_steps:
            print("  [Error] No installation steps could be generated or loaded.")
            return {"status": "error", "error": "no installation steps", "tokens_used": TOKENS.as_dict()}

    except Exception as e:
        print(f"  [Error] Failed to initialize active steps: {e}")
        return {"status": "error", "error": str(e), "tokens_used": TOKENS.as_dict()}

    sep = "═" * 56
    print(sep)
    print(f"  {system_name.upper()} INSTALLER AGENT — {len(active_steps)} steps")
    print(sep)

    try:
        client.connect(hostname=host, port=port,
                       username=username, password=ssh_password)

        _, uid = execute_ssh(client, "id -u", sudo_password=None)
        if uid.strip() == "0":
            print("  [Info] Running as root — sudo calls will not require a password.")

        # ── Pre-flight: sanitise broken apt sources ───────────────────────
        # Previous failed installations (e.g. ROS2) can leave broken
        # .list files that reference non-existent keyrings, poisoning
        # every subsequent `apt-get update`.  Clean them up.
        print("\n  ── Pre-flight: checking apt sources health ──")
        apt_exit, apt_out = execute_ssh(client, "sudo apt-get update -qq 2>&1", sudo_password)
        if apt_exit != 0 and "Conflicting values" in apt_out:
            print("  [Fix] Detected conflicting apt source. Attempting cleanup...")
            # Find the offending .list file from the error message
            conflicting_match = re.search(r'source\s+(\S+)', apt_out)
            # Remove known problematic source files
            cleanup_cmds = [
                "sudo rm -f /etc/apt/sources.list.d/ros2*.list",
                "sudo rm -f /etc/apt/sources.list.d/ros2.list",
                "sudo rm -f /etc/apt/sources.list.d/ros2-latest.list",
                "sudo apt-get update -qq 2>&1",
            ]
            for cmd in cleanup_cmds:
                execute_ssh(client, cmd, sudo_password)
            # Verify apt is healthy now
            v_exit, v_out = execute_ssh(client, "sudo apt-get update -qq 2>&1", sudo_password)
            if v_exit == 0:
                print("  [Fix] ✓ apt sources cleaned up successfully.")
            else:
                print(f"  [Fix] ⚠ apt sources may still have issues: {v_out[:200]}")
        elif apt_exit == 0:
            print("  [OK] apt sources are healthy.")

        # ── Execute steps ─────────────────────────────────────────────────
        skills = load_skills()
        abort = False
        for idx, step in enumerate(active_steps):
            if abort:
                # Mark remaining steps as skipped
                step_results.append({"step": step, "success": False, "history": [],
                                     "skipped": True})
                continue

            success, history = execute_step(
                client, step, os_flavor, conn, session_id,
                sudo_password, idx + 1, len(active_steps), skills, use_llm
            )

            # Check if LLM fixed the step! If the last history attempt succeeded and its command differs
            # from the original step command, we update the active_steps array with the perfected command!
            if success and history and history[-1]["exit_code"] == 0:
                fixed_cmd = history[-1]["command"]
                if fixed_cmd != step["command"]:
                    print(f"  [Planner] Perfecting step '{step['name']}' in cache array with learned fix.")
                    active_steps[idx]["command"] = fixed_cmd

            step_results.append({"step": step, "success": success, "history": history,
                                  "skipped": False})

            if not success and step.get("critical", True):
                print(f"\n  🚨 Critical step failed: '{step['name']}'")
                print(f"     All {MAX_RETRIES} retry attempts exhausted.")
                print(f"     Installation cannot continue.")
                print(f"     See {REPORT_PATH} and {DB_PATH} for full details.")
                log_event(conn, session_id, "installer_agent", "abort",
                          step["name"], MAX_RETRIES,
                          "Critical step failed — aborting installation", -1, "aborted")
                abort = True

        if not abort:
            installation_ok = True
            try:
                cache_path.write_text(json.dumps(active_steps, indent=4), encoding="utf-8")
                print(f"  [Planner] Successfully saved perfected installation steps to {cache_path}")
            except Exception as e:
                print(f"  [Planner] Failed to save cache: {e}")

    except Exception as e:
        print(f"\nFatal SSH error: {e}")
        log_event(conn, session_id, "installer_agent", "fatal_error",
                  "ssh", 0, str(e), -1, "failed")
    finally:
        client.close()
        overall_status = "success" if installation_ok else "failed"
        close_session(conn, session_id, overall_status)

    # ── Generate session report ───────────────────────────────────────────
    print(f"\n{'─'*56}")
    print("  Generating session report...")
    generate_report(
        session_id, inventory, os_flavor,
        step_results, start_time
    )
    print(f"  Report saved to: {REPORT_PATH}")

    # ── Final summary ─────────────────────────────────────────────────────
    print(f"\n{sep}")
    print("  INSTALLER AGENT — FINAL SUMMARY")
    print(sep)

    for result in step_results:
        if result.get("skipped"):
            icon, label = "–", "skipped"
        elif result["success"]:
            icon, label = "✓", "success"
        else:
            icon, label = "✗", "FAILED"
        crit = " [critical]" if result["step"].get("critical") else ""
        print(f"  {icon}  {result['step']['name']}{crit}  →  {label}")

    print()
    if installation_ok:
        print(f"  ✅ {system_name} installation complete.")
        print(f"     Session report: {REPORT_PATH}")
        print(f"     Audit log:      {DB_PATH}")
    else:
        failed_steps = [
            r["step"]["name"] for r in step_results
            if not r["success"] and not r.get("skipped")
        ]
        if failed_steps:
            print(f"  ✗ Failed steps: {failed_steps}")
        print(f"\n  Full LLM retry history: {DB_PATH}")
        print(f"  Session report:         {REPORT_PATH}")
    print(sep)

    return {
        "status": "ok" if installation_ok else "error",
        "result_path": REPORT_PATH,
        "summary": {
            "steps": {
                r["step"]["name"]: "success" if r["success"] else "skipped" if r.get("skipped") else "failed"
                for r in step_results
            },
            "verification_passed": installation_ok,
        },
        "tokens_used": TOKENS.as_dict(),
    }



import argparse
def parse_args() -> dict:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", help="Override the automatically discovered llama.cpp model")
    args, _ = parser.parse_known_args()
    return {"model": args.model}

def main():
    print(json.dumps(run(parse_args()), indent=2))


if __name__ == "__main__":
    main()

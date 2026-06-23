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
# 2. Aurelis Installation Steps
# ==========================================
# Each step is a dict:
#   name      — human-readable label (used in logs and report)
#   command   — exact shell command to run on the target server
#   sudo      — True if this command needs root
#   critical  — True = abort entire installation if this step fails after all retries
#               False = log failure and continue (for non-critical setup steps)
#   verify    — optional command to confirm the step actually worked (runs only if command succeeds)

INSTALL_STEPS = [
    # ── Pre-flight ─────────────────────────────────────────────────────
    {
        "name":     "Verify Docker is running",
        "command":  "systemctl is-active docker",
        "sudo":     False,
        "critical": True,
        "verify":   None,
    },
    {
        "name":     "Verify kubectl is accessible",
        "command":  "kubectl version --client 2>&1",
        "sudo":     False,
        "critical": True,
        "verify":   None,
    },
    {
        "name":     "Verify Helm is accessible",
        "command":  "helm version --short 2>&1",
        "sudo":     False,
        "critical": True,
        "verify":   None,
    },

    # ── Cluster setup ──────────────────────────────────────────────────
    # kind creates a local single-node Kubernetes cluster for Aurelis.
    # Skip this block if you're deploying to an existing cluster.
    {
        "name":     "Install kind (Kubernetes IN Docker)",
        "command":  "curl -Lo /tmp/kind https://kind.sigs.k8s.io/dl/v0.23.0/kind-linux-amd64 && chmod +x /tmp/kind && sudo mv /tmp/kind /usr/local/bin/kind",
        "sudo":     False,   # sudo is embedded in the command
        "critical": True,
        "verify":   "kind version",
    },
    {
        "name":     "Create kind cluster for Aurelis",
        "command":  "kind create cluster --name aurelis --wait 120s 2>&1 || echo 'Cluster may already exist'",
        "sudo":     False,
        "critical": True,
        "verify":   "kubectl cluster-info --context kind-aurelis 2>&1",
    },

    # ── Namespace + RBAC ───────────────────────────────────────────────
    {
        "name":     "Create aurelis namespace",
        "command":  "kubectl create namespace aurelis 2>&1 || echo 'Namespace already exists'",
        "sudo":     False,
        "critical": True,
        "verify":   "kubectl get namespace aurelis",
    },

    # ── Helm chart deployment ──────────────────────────────────────────
    # Replace the repo URL and chart name with the actual Nokia Aurelis values.
    # The placeholder below follows Nokia's documented pattern:
    #   helm repo add nokia <NOKIA_CHART_REPO_URL>
    #   helm install aurelis nokia/aurelis-command-center -n aurelis -f values.yaml
    {
        "name":     "Add Nokia Aurelis Helm repository",
        "command":  "helm repo add nokia https://charts.nokia.com/aurelis && helm repo update",
        "sudo":     False,
        "critical": True,
        "verify":   "helm repo list | grep nokia",
    },
    {
        "name":     "Deploy Aurelis Command Center via Helm",
        "command":  "helm upgrade --install aurelis nokia/aurelis-command-center "
                    "--namespace aurelis "
                    "--create-namespace "
                    "--wait "
                    "--timeout 10m "
                    "2>&1",
        "sudo":     False,
        "critical": True,
        "verify":   "helm status aurelis -n aurelis 2>&1 | grep STATUS",
    },

    # ── Post-deploy checks ─────────────────────────────────────────────
    {
        "name":     "Wait for Aurelis pods to be ready",
        "command":  "kubectl wait --for=condition=ready pod "
                    "--all -n aurelis "
                    "--timeout=300s 2>&1",
        "sudo":     False,
        "critical": True,
        "verify":   "kubectl get pods -n aurelis",
    },
    {
        "name":     "Check Aurelis service endpoints",
        "command":  "kubectl get svc -n aurelis 2>&1",
        "sudo":     False,
        "critical": False,
        "verify":   None,
    },
]

NS3_INSTALL_STEPS = [
    {
        "name":     "Clone NS-3 Development Repository",
        "command":  "rm -rf /tmp/ns-3-dev && git clone https://gitlab.com/nsnam/ns-3-dev.git /tmp/ns-3-dev",
        "sudo":     False,
        "critical": True,
        "verify":   "ls -la /tmp/ns-3-dev/ns3",
    },
    {
        "name":     "Configure NS-3",
        "command":  "cd /tmp/ns-3-dev && ./ns3 configure --enable-examples --enable-tests",
        "sudo":     False,
        "critical": True,
        "verify":   None,
    },
    {
        "name":     "Build NS-3 Simulator",
        "command":  "cd /tmp/ns-3-dev && ./ns3 build",
        "sudo":     False,
        "critical": True,
        "verify":   "ls -la /tmp/ns-3-dev/build",
    },
    {
        "name":     "Test NS-3 Core",
        "command":  "cd /tmp/ns-3-dev && ./test.py --no-build --suite=core",
        "sudo":     False,
        "critical": False,
        "verify":   None,
    }
]

ROS2_INSTALL_STEPS = [
    {
        "name":     "Update Apt and Install Curl",
        "command":  "sudo apt-get update -y && sudo apt-get install curl -y",
        "sudo":     True,
        "critical": True,
        "verify":   "curl --version",
    },
    {
        "name":     "Add ROS2 GPG Key",
        "command":  "sudo curl -sSL https://raw.githubusercontent.com/ros/rosdistro/master/ros.key -o /usr/share/keyrings/ros-archive-keyring.gpg",
        "sudo":     True,
        "critical": True,
        "verify":   "ls /usr/share/keyrings/ros-archive-keyring.gpg",
    },
    {
        "name":     "Add ROS2 Repository",
        "command":  "echo \"deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/ros-archive-keyring.gpg] http://packages.ros.org/ros2/ubuntu $(grep UBUNTU_CODENAME /etc/os-release | cut -d= -f2) main\" | sudo tee /etc/apt/sources.list.d/ros2.list > /dev/null",
        "sudo":     True,
        "critical": True,
        "verify":   "cat /etc/apt/sources.list.d/ros2.list",
    },
    {
        "name":     "Install ROS2 Humble Base",
        "command":  "sudo apt-get update -y && sudo apt-get install -y ros-humble-ros-base",
        "sudo":     True,
        "critical": True,
        "verify":   "ls /opt/ros/humble",
    }
]

# ==========================================
# 3. Post-Installation Verification Suite
# ==========================================
# Run after all install steps succeed.
# Each check has a name, command, and expected_exit_code.
# All checks run regardless of individual failures — failures are collected and reported.

VERIFY_SUITE = [
    {
        "name":    "All Aurelis pods running",
        "command": "kubectl get pods -n aurelis --no-headers | awk '{print $3}' | grep -v Running | wc -l",
        "expect":  "0",   # expect zero non-Running pods
    },
    {
        "name":    "Helm release status is deployed",
        "command": "helm status aurelis -n aurelis --output json 2>&1 | python3 -c \"import sys,json; d=json.load(sys.stdin); print(d['info']['status'])\"",
        "expect":  "deployed",
    },
    {
        "name":    "Aurelis namespace has running services",
        "command": "kubectl get svc -n aurelis --no-headers | wc -l",
        "expect":  None,   # just check exit code 0, any output is fine
    },
    {
        "name":    "No crash-looping pods in last 60s",
        "command": "kubectl get pods -n aurelis --no-headers | awk '{print $3}' | grep -c CrashLoopBackOff || true",
        "expect":  "0",
    },
    {
        "name":    "kubectl can reach the cluster API",
        "command": "kubectl cluster-info 2>&1 | head -1",
        "expect":  None,
    },
]


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
4. Respond with ONLY raw JSON — no markdown, no preamble.

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
def run_verify_suite(
    client: paramiko.SSHClient,
    conn: sqlite3.Connection,
    session_id: str,
    sudo_password: str | None,
) -> tuple[bool, list[dict]]:
    """
    Run all post-install checks. Returns (all_passed, results_list).
    """
    print(f"\n  Waiting {POST_INSTALL_SETTLE_TIME}s for services to stabilise...")
    time.sleep(POST_INSTALL_SETTLE_TIME)

    print(f"\n{'─'*54}")
    print("  POST-INSTALLATION VERIFICATION")
    print(f"{'─'*54}")

    results = []
    all_passed = True

    for check in VERIFY_SUITE:
        name    = check["name"]
        cmd     = check["command"]
        expect  = check.get("expect")

        exit_code, output = execute_ssh(client, cmd, sudo_password=None)

        passed = exit_code == 0
        if expect is not None:
            passed = passed and output.strip() == expect.strip()

        status = "PASS" if passed else "FAIL"
        icon   = "✓" if passed else "✗"

        print(f"  {icon}  {name}")
        if not passed:
            print(f"       expected={expect!r}  got={output[:120]!r}")
            all_passed = False

        log_event(conn, session_id, "installer_agent", "verify",
                  name, 0, output, exit_code, status.lower())

        results.append({
            "name":      name,
            "command":   cmd,
            "output":    output,
            "exit_code": exit_code,
            "expected":  expect,
            "passed":    passed,
        })

    return all_passed, results


# ==========================================
# 11. Session Report
# ==========================================
def generate_report(
    session_id: str,
    inventory: dict,
    os_flavor: str,
    step_results: list[dict],   # [{"step": dict, "success": bool, "history": list}]
    verify_results: list[dict],
    start_time: datetime.datetime,
) -> str:
    """Build a markdown session report and save it to REPORT_PATH."""
    end_time = datetime.datetime.now()
    duration = str(end_time - start_time).split(".")[0]

    overall = all(r["success"] for r in step_results if r["step"].get("critical", True))
    status_str = "✅ SUCCESS" if overall else "❌ FAILED"

    lines = [
        f"# ADRA Session Report — Nokia Aurelis Installer",
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
        f"## Post-Installation Verification",
        f"",
        f"| Check | Result | Output |",
        f"|---|---|---|",
    ]

    for vr in verify_results:
        icon = "✓" if vr["passed"] else "✗"
        lines.append(
            f"| {vr['name']} | {icon} {'PASS' if vr['passed'] else 'FAIL'} | `{vr['output'][:80]}` |"
        )

    lines += [
        f"",
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
    verify_results: list[dict] = []
    installation_ok = False

    try:
        reqs = json.loads(Path("requirements.json").read_text(encoding="utf-8"))
        source = reqs.get("source", "").lower()
        if "ns-3" in source:
            active_steps = NS3_INSTALL_STEPS
            system_name = "NS-3 Simulator"
        elif "ros2" in source:
            active_steps = ROS2_INSTALL_STEPS
            system_name = "ROS2 Humble"
        else:
            active_steps = INSTALL_STEPS
            system_name = "ADRA Aurelis"
    except Exception:
        active_steps = INSTALL_STEPS
        system_name = "ADRA Aurelis"

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
                sudo_password, idx + 1, len(active_steps), skills
            )

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
            # ── Post-install verification ─────────────────────────────────
            installation_ok, verify_results = run_verify_suite(
                client, conn, session_id, sudo_password
            )

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
        step_results, verify_results, start_time
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
        print("  ✅ Aurelis installation complete.")
        print(f"     Session report: {REPORT_PATH}")
        print(f"     Audit log:      {DB_PATH}")
    else:
        failed_steps = [
            r["step"]["name"] for r in step_results
            if not r["success"] and not r.get("skipped")
        ]
        failed_checks = [v["name"] for v in verify_results if not v["passed"]]
        if failed_steps:
            print(f"  ✗ Failed steps: {failed_steps}")
        if failed_checks:
            print(f"  ✗ Failed checks: {failed_checks}")
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

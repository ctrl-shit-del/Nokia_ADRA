import json
import re
import sqlite3
import paramiko
import getpass
import requests
import datetime

# ==========================================
# 1. Configuration
# ==========================================
SSH_HOST     = "127.0.0.1"
SSH_PORT     = 22
SSH_USER     = "mystic"

# Set True if the SSH user has passwordless sudo configured.
# Run: echo "mystic ALL=(ALL) NOPASSWD:ALL" | sudo tee /etc/sudoers.d/mystic
PASSWORDLESS_SUDO = True

OLLAMA_URL   = "http://localhost:11434/api/generate"
MODEL_NAME   = "gemma4:31b-cloud"

MAX_RETRIES  = 5    # LLM-guided retry attempts per failing command
DB_PATH      = "adra_audit.db"

# ==========================================
# 2. Skills Library
# ==========================================
# Each skill defines check / install (per-OS) / verify commands.
# install["all"] is used when the command is OS-agnostic.
# Add new packages here — the Requirements Agent will eventually
# auto-generate these from the Nokia docs.

SKILLS: dict[str, dict] = {
    "python": {
        "description": "Python 3 interpreter",
        "check_cmd":   "python3 --version 2>&1",
        "install": {
            "ubuntu": [
                "apt-get update -y",
                "apt-get install -y python3 python3-pip python3-venv",
            ],
            "rhel": [
                "yum install -y python3 python3-pip",
            ],
        },
        "verify_cmd": "python3 --version 2>&1",
    },

    "docker": {
        "description": "Docker container runtime",
        "check_cmd":   "docker --version 2>&1",
        "install": {
            "ubuntu": [
                "apt-get update -y",
                "apt-get install -y ca-certificates curl gnupg",
                "install -m 0755 -d /etc/apt/keyrings",
                "curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg",
                "chmod a+r /etc/apt/keyrings/docker.gpg",
                # VERSION_CODENAME picks the right repo for Ubuntu 22/24/26
                'echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] '
                'https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" '
                '| tee /etc/apt/sources.list.d/docker.list > /dev/null',
                "apt-get update -y",
                "apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin",
                "systemctl enable docker --now 2>&1 || true",
            ],
            "rhel": [
                "yum install -y yum-utils",
                "yum-config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo",
                "yum install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin",
                "systemctl enable docker --now",
            ],
        },
        "verify_cmd": "docker --version 2>&1",
    },

    "kubernetes": {
        "description": "kubectl — Kubernetes CLI client",
        "check_cmd":   "kubectl version --client 2>&1",
        "install": {
            "ubuntu": [
                "apt-get update -y",
                "apt-get install -y apt-transport-https ca-certificates curl gpg",
                "curl -fsSL https://pkgs.k8s.io/core:/stable:/v1.29/deb/Release.key | gpg --dearmor -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg",
                'echo "deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] '
                'https://pkgs.k8s.io/core:/stable:/v1.29/deb/ /" '
                '| tee /etc/apt/sources.list.d/kubernetes.list',
                "apt-get update -y",
                "apt-get install -y kubectl",
            ],
            "rhel": [
                "printf '[kubernetes]\\nname=Kubernetes\\n"
                "baseurl=https://pkgs.k8s.io/core:/stable:/v1.29/rpm/\\n"
                "enabled=1\\ngpgcheck=1\\n"
                "gpgkey=https://pkgs.k8s.io/core:/stable:/v1.29/rpm/repodata/repomd.xml.key\\n'"
                " | tee /etc/yum.repos.d/kubernetes.repo",
                "yum install -y kubectl",
            ],
        },
        "verify_cmd": "kubectl version --client 2>&1",
    },

    "helm": {
        "description": "Helm — Kubernetes package manager",
        "check_cmd":   "helm version --short 2>&1",
        "install": {
            # Helm's official install script works on both Ubuntu and RHEL
            "all": [
                "curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 -o /tmp/get_helm.sh",
                "chmod +x /tmp/get_helm.sh",
                "bash /tmp/get_helm.sh",
            ],
        },
        "verify_cmd": "helm version --short 2>&1",
    },
}


# ==========================================
# 3. JSON Parsing
# ==========================================
def extract_json(raw: str) -> dict:
    """
    Robustly parses JSON from LLM output that may contain:
    - Markdown fences: ```json ... ```
    - Thinking traces: <think>...</think>
    - Prose before/after the JSON object
    """
    if not raw:
        raise ValueError("Empty LLM response")
    text = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    text = re.sub(r"```(?:json)?\s*", "", text)
    text = re.sub(r"```", "", text).strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"No JSON object found in response:\n{raw[:300]}")
    return json.loads(match.group())


# ==========================================
# 4. LLM + SSH Helpers
# ==========================================
def call_llm(prompt: str) -> str | None:
    payload = {"model": MODEL_NAME, "prompt": prompt, "stream": False}
    try:
        resp = requests.post(OLLAMA_URL, json=payload, timeout=120)
        resp.raise_for_status()
        return resp.json()["response"]
    except requests.exceptions.RequestException as e:
        print(f"  [LLM Error] {e}")
        return None


def execute_ssh(client: paramiko.SSHClient, command: str) -> tuple[int, str]:
    """Run a command over SSH. Returns (exit_code, combined_output)."""
    if PASSWORDLESS_SUDO and not command.startswith("sudo "):
        command = f"sudo {command}"
    print(f"  [SSH] {command[:100]}{'...' if len(command) > 100 else ''}")
    _, stdout, stderr = client.exec_command(command)
    exit_code = stdout.channel.recv_exit_status()
    out = stdout.read().decode("utf-8").strip()
    err = stderr.read().decode("utf-8").strip()
    combined = (out + "\n" + err).strip() if err else out
    if exit_code != 0:
        print(f"  [Exit {exit_code}] {combined[:200]}")
    else:
        print(f"  [OK] {combined[:80]}")
    return exit_code, combined


# ==========================================
# 5. SQLite Audit Logging
# ==========================================
def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.executescript("""
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
         str(content)[:4000], exit_code, status)
    )
    conn.commit()


def open_session(conn, session_id: str, host: str):
    conn.execute(
        "INSERT INTO sessions VALUES (?,?,NULL,?,?)",
        (session_id, datetime.datetime.now().isoformat(), host, "running")
    )
    conn.commit()


def close_session(conn, session_id: str, status: str):
    conn.execute(
        "UPDATE sessions SET ended_at=?, status=? WHERE id=?",
        (datetime.datetime.now().isoformat(), status, session_id)
    )
    conn.commit()


# ==========================================
# 6. OS Detection
# ==========================================
def detect_os_flavor(inventory: dict) -> str:
    """Infer 'ubuntu' or 'rhel' from the os_name field in inventory.json."""
    for item in inventory.get("software", []):
        if item["item"] == "os_name" and item.get("found"):
            name = item["found"].lower()
            if "ubuntu" in name or "debian" in name:
                return "ubuntu"
            if any(k in name for k in ("rhel", "red hat", "centos", "fedora", "rocky", "alma")):
                return "rhel"
    print("  [Warning] Could not detect OS flavor from inventory. Defaulting to ubuntu.")
    return "ubuntu"


# ==========================================
# 7. LLM Retry Loop
# ==========================================
def ask_llm_for_fix(package: str, os_flavor: str, attempt_history: list[dict]) -> dict | None:
    """
    Given the full attempt history for one failing command, ask the LLM to
    diagnose the CURRENT (most recent) failure and suggest ONE shell command.

    attempt_history entries:
      {"attempt": int, "command": str, "exit_code": int, "output": str,
       "llm_diagnosis": str | None, "llm_suggested": str | None}
    """
    history_lines = []
    for entry in attempt_history:
        history_lines.append(f"\nAttempt {entry['attempt']}:")
        history_lines.append(f"  Command:   {entry['command']}")
        history_lines.append(f"  Exit code: {entry['exit_code']}")
        history_lines.append(f"  Output:    {entry['output'][:500]}")
        if entry.get("llm_diagnosis"):
            history_lines.append(f"  LLM said:  {entry['llm_diagnosis']}")
        if entry.get("llm_suggested"):
            history_lines.append(f"  LLM tried: {entry['llm_suggested']}")

    current = attempt_history[-1]

    prompt = f"""You are a Linux system administration expert.
A deployment script failed while installing a package. Diagnose the error and suggest ONE shell command to resolve it.

CONTEXT:
  OS:        {os_flavor}
  Package:   {package}
  Attempt:   {current['attempt']} of {MAX_RETRIES}

ATTEMPT HISTORY (full chain, most recent is the current failure):
{''.join(history_lines)}

RULES:
1. Focus on diagnosing the MOST RECENT error — not the original one.
2. Do NOT suggest a command that already appears in the attempt history above.
3. Suggest ONE concrete shell command. No multi-command strings unless unavoidable.
4. Respond with ONLY a raw JSON object — no markdown fences, no explanation.

JSON FORMAT:
{{
  "diagnosis":         "root cause of the current failure in one sentence",
  "suggested_command": "exact shell command to run",
  "reasoning":         "why this command will fix the current error",
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
# 8. Single-Package Installer
# ==========================================
def get_install_commands(skill: dict, os_flavor: str) -> list[str] | None:
    install = skill.get("install", {})
    return install.get(os_flavor) or install.get("all")


def install_package(
    client: paramiko.SSHClient,
    pkg_name: str,
    skill: dict,
    os_flavor: str,
    conn: sqlite3.Connection,
    session_id: str,
) -> str:
    """
    Install one package end-to-end.
    Returns: "success" | "failed" | "skipped" | "already_installed"
    """
    sep = "─" * 52
    print(f"\n┌{sep}┐")
    print(f"│  📦  {pkg_name}  —  {skill['description']:<40}│")
    print(f"└{sep}┘")

    # Pre-flight check: is it already installed at an acceptable version?
    check_cmd = skill.get("check_cmd", "")
    if check_cmd:
        exit_code, output = execute_ssh(client, check_cmd)
        if exit_code == 0:
            print(f"  [Already installed] {output}")
            log_event(conn, session_id, "packages_agent", "check",
                      pkg_name, 0, output, exit_code, "already_installed")
            return "already_installed"

    install_cmds = get_install_commands(skill, os_flavor)
    if not install_cmds:
        print(f"  [Skip] No install commands defined for OS '{os_flavor}'.")
        return "skipped"

    # ── Run install sequence ──────────────────────────────────────────────
    for step_idx, base_cmd in enumerate(install_cmds):
        total = len(install_cmds)
        print(f"\n  ── Step {step_idx+1}/{total} ──")
        log_event(conn, session_id, "packages_agent", "command",
                  pkg_name, 1, base_cmd, None, "running")

        exit_code, output = execute_ssh(client, base_cmd)
        log_event(conn, session_id, "packages_agent", "command",
                  pkg_name, 1, base_cmd, exit_code,
                  "success" if exit_code == 0 else "fail")

        if exit_code == 0:
            continue  # Step succeeded, move on

        # ── Step failed — LLM retry loop ─────────────────────────────────
        print(f"\n  ⚠  Step {step_idx+1} failed. Entering LLM retry loop (max {MAX_RETRIES} attempts)...")

        # Attempt 1 is the original command that just failed
        attempt_history: list[dict] = [{
            "attempt":       1,
            "command":       base_cmd,
            "exit_code":     exit_code,
            "output":        output,
            "llm_diagnosis": None,
            "llm_suggested": None,
        }]

        resolved = False
        for attempt_num in range(2, MAX_RETRIES + 1):
            print(f"\n  [Attempt {attempt_num}/{MAX_RETRIES}] Consulting LLM...")

            llm_result = ask_llm_for_fix(pkg_name, os_flavor, attempt_history)

            if not llm_result:
                print(f"  [LLM] No response. Skipping attempt {attempt_num}.")
                log_event(conn, session_id, "packages_agent", "llm_error",
                          pkg_name, attempt_num, "No LLM response", None, "fail")
                attempt_history.append({
                    "attempt": attempt_num, "command": "(no LLM response)",
                    "exit_code": -1, "output": "", "llm_diagnosis": None, "llm_suggested": None
                })
                continue

            diagnosis      = llm_result.get("diagnosis", "")
            suggested_cmd  = llm_result.get("suggested_command", "").strip()
            reasoning      = llm_result.get("reasoning", "")
            confidence     = llm_result.get("confidence", 0.0)

            print(f"  [LLM Diagnosis] {diagnosis}")
            print(f"  [LLM Suggests]  {suggested_cmd}")
            print(f"  [Confidence]    {confidence:.2f}")

            log_event(conn, session_id, "packages_agent", "llm_response",
                      pkg_name, attempt_num, json.dumps(llm_result), None, "retry")

            if not suggested_cmd:
                print(f"  [LLM] Returned empty command. Skipping.")
                continue

            # Record what the LLM said before we run the command
            attempt_history[-1]["llm_diagnosis"] = diagnosis
            attempt_history[-1]["llm_suggested"] = suggested_cmd

            exit_code, output = execute_ssh(client, suggested_cmd)
            log_event(conn, session_id, "packages_agent", "command",
                      pkg_name, attempt_num, suggested_cmd, exit_code,
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
                resolved = True
                break
            else:
                print(f"  Still failing. Continuing retry loop...")

        if not resolved:
            print(f"\n  ✗ All {MAX_RETRIES} attempts exhausted for step {step_idx+1} of '{pkg_name}'.")
            print(f"    Check {DB_PATH} for the full LLM conversation history.")
            log_event(conn, session_id, "packages_agent", "result",
                      pkg_name, MAX_RETRIES, "All retries exhausted", None, "exhausted")
            return "failed"

    # ── Verify installation ───────────────────────────────────────────────
    verify_cmd = skill.get("verify_cmd", "")
    if verify_cmd:
        print(f"\n  [Verify] {verify_cmd}")
        exit_code, output = execute_ssh(client, verify_cmd)
        if exit_code == 0:
            print(f"  ✓ Verified: {output}")
            log_event(conn, session_id, "packages_agent", "result",
                      pkg_name, 0, f"Verified: {output}", exit_code, "success")
            return "success"
        else:
            print(f"  ✗ Verify failed: {output}")
            log_event(conn, session_id, "packages_agent", "result",
                      pkg_name, 0, f"Verify failed: {output}", exit_code, "fail")
            return "failed"

    return "success"


# ==========================================
# 9. Main
# ==========================================
def main():
    # ── Load inventory ────────────────────────────────────────────────────
    try:
        with open("inventory.json") as f:
            inventory = json.load(f)
    except FileNotFoundError:
        print("Error: inventory.json not found. Run inventory_agent.py first.")
        return

    os_flavor = detect_os_flavor(inventory)
    print(f"\nDetected OS flavor: {os_flavor}")

    # ── Separate hardware alerts from software install list ───────────────
    hardware_issues = [
        item for item in inventory.get("hardware", [])
        if item["status"] in ("Missing", "Insufficient")
    ]
    software_todo = [
        item for item in inventory.get("software", [])
        if item["status"] in ("Missing", "Insufficient")
    ]

    if hardware_issues:
        print("\n" + "━" * 54)
        print("  ⚠️   HARDWARE REQUIREMENTS NOT MET")
        print("  These cannot be auto-resolved by this agent.")
        print("━" * 54)
        for item in hardware_issues:
            print(f"  {item['item']:<12}  required={item['required']}  "
                  f"found={item['found']}  → {item['status']}")
        print("\n  Provision adequate hardware before running installer_agent.py.")
        print("━" * 54)

        answer = input("\n  Continue with software installation anyway? (y/N): ").strip().lower()
        if answer != "y":
            print("Aborting.")
            return

    if not software_todo:
        print("\n✓ All software requirements already met. Nothing to install.")
        return

    print(f"\nSoftware to install / upgrade: {[i['item'] for i in software_todo]}")

    # ── SSH + DB setup ────────────────────────────────────────────────────
    ssh_password = getpass.getpass(f"\nEnter SSH password for {SSH_USER}@{SSH_HOST}: ")
    conn = init_db()
    session_id = f"pkg_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}"
    open_session(conn, session_id, SSH_HOST)

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    results: dict[str, str] = {}

    try:
        client.connect(hostname=SSH_HOST, port=SSH_PORT,
                       username=SSH_USER, password=ssh_password)

        for inv_item in software_todo:
            pkg_name = inv_item["item"]
            skill = SKILLS.get(pkg_name)

            if not skill:
                print(f"\n  [Skip] No skill defined for '{pkg_name}'. "
                      f"Add it to the SKILLS dict at the top of this file.")
                results[pkg_name] = "no_skill"
                log_event(conn, session_id, "packages_agent", "result",
                          pkg_name, 0, "No skill defined", None, "skipped")
                continue

            result = install_package(client, pkg_name, skill,
                                     os_flavor, conn, session_id)
            results[pkg_name] = result

    except Exception as e:
        print(f"\nFatal SSH error: {e}")
    finally:
        client.close()
        overall = "failed" if any(r == "failed" for r in results.values()) else "success"
        close_session(conn, session_id, overall)
        conn.close()

    # ── Final summary ─────────────────────────────────────────────────────
    print("\n")
    print("┌" + "─" * 52 + "┐")
    print("│          PACKAGES AGENT — FINAL SUMMARY            │")
    print("└" + "─" * 52 + "┘")

    icons = {
        "success":           "✓",
        "already_installed": "✓",
        "failed":            "✗",
        "skipped":           "–",
        "no_skill":          "?",
    }
    for pkg, result in results.items():
        icon = icons.get(result, "?")
        print(f"  {icon}  {pkg:<18} {result}")

    failed = [p for p, r in results.items() if r == "failed"]
    no_skill = [p for p, r in results.items() if r == "no_skill"]

    print()
    if failed:
        print(f"  ✗ {len(failed)} package(s) failed after {MAX_RETRIES} retries: {failed}")
        print(f"    Full LLM retry history saved to: {DB_PATH}")
        print("    ⚠  installer_agent.py should NOT run until these are resolved.")
    else:
        print("  ✓ All packages installed successfully.")
        print("    Ready to run: python3 installer_agent.py")

    if no_skill:
        print(f"\n  ? Skills missing for: {no_skill}")
        print("    Add entries to the SKILLS dict in this file.")

    print()


if __name__ == "__main__":
    main()
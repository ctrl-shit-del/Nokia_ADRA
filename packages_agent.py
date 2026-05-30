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
SSH_HOST = "127.0.0.1"
SSH_PORT = 22
SSH_USER = "mystic"

# Set True ONLY if sudoers has NOPASSWD configured for SSH_USER:
#   echo "mystic ALL=(ALL) NOPASSWD:ALL" | sudo tee /etc/sudoers.d/mystic
# When False (default), sudo -S reads the password from stdin — no TTY needed.
PASSWORDLESS_SUDO = False

OLLAMA_URL  = "http://localhost:11434/api/generate"
MODEL_NAME  = "gemma4:31b-cloud"

MAX_RETRIES = 5
DB_PATH     = "adra_audit.db"

# ==========================================
# 2. Skills Library
# ==========================================
# Rules for writing skill commands:
# - Add "sudo" explicitly to commands that write to system paths (/etc, /usr, ...)
#   or call apt-get / yum / systemctl.
# - Do NOT add sudo to: curl (downloading to /tmp), chmod on /tmp files,
#   check_cmd, verify_cmd.
# - Avoid "cmd1 | sudo cmd2" pipelines — split them into two separate steps
#   so the retry loop can target individual failures.

SKILLS: dict[str, dict] = {
    "python": {
        "description": "Python 3 interpreter",
        "check_cmd":   "python3 --version 2>&1",
        "install": {
            "ubuntu": [
                "sudo apt-get update -y",
                "sudo apt-get install -y python3 python3-pip python3-venv",
            ],
            "rhel": [
                "sudo yum install -y python3 python3-pip",
            ],
        },
        "verify_cmd": "python3 --version 2>&1",
    },

    "docker": {
        "description": "Docker container runtime",
        "check_cmd":   "docker --version 2>&1",
        "install": {
            "ubuntu": [
                "sudo apt-get update -y",
                "sudo apt-get install -y ca-certificates curl gnupg",
                "sudo install -m 0755 -d /etc/apt/keyrings",
                # Download GPG key to /tmp first, then process with sudo separately
                # (avoids "cmd | sudo cmd" which our sudo -S handler can't intercept mid-pipe)
                "curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /tmp/docker.gpg",
                "sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg /tmp/docker.gpg",
                "sudo chmod a+r /etc/apt/keyrings/docker.gpg",
                # sudo bash -c wraps the whole echo+redirect so only one sudo call is needed
                'sudo bash -c \'echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" > /etc/apt/sources.list.d/docker.list\'',
                "sudo apt-get update -y",
                "sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin",
                "sudo systemctl enable docker --now 2>&1 || true",
            ],
            "rhel": [
                "sudo yum install -y yum-utils",
                "sudo yum-config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo",
                "sudo yum install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin",
                "sudo systemctl enable docker --now",
            ],
        },
        "verify_cmd": "docker --version 2>&1",
    },

    "kubernetes": {
        "description": "kubectl — Kubernetes CLI client",
        "check_cmd":   "kubectl version --client 2>&1",
        "install": {
            "ubuntu": [
                "sudo apt-get update -y",
                "sudo apt-get install -y apt-transport-https ca-certificates curl gpg",
                # Download key to /tmp, then sudo-process it
                "curl -fsSL https://pkgs.k8s.io/core:/stable:/v1.29/deb/Release.key -o /tmp/k8s.gpg",
                "sudo gpg --dearmor -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg /tmp/k8s.gpg",
                'sudo bash -c \'echo "deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] https://pkgs.k8s.io/core:/stable:/v1.29/deb/ /" > /etc/apt/sources.list.d/kubernetes.list\'',
                "sudo apt-get update -y",
                "sudo apt-get install -y kubectl",
            ],
            "rhel": [
                "sudo bash -c 'printf \"[kubernetes]\\nname=Kubernetes\\nbaseurl=https://pkgs.k8s.io/core:/stable:/v1.29/rpm/\\nenabled=1\\ngpgcheck=1\\ngpgkey=https://pkgs.k8s.io/core:/stable:/v1.29/rpm/repodata/repomd.xml.key\\n\" > /etc/yum.repos.d/kubernetes.repo'",
                "sudo yum install -y kubectl",
            ],
        },
        "verify_cmd": "kubectl version --client 2>&1",
    },

    "helm": {
        "description": "Helm — Kubernetes package manager",
        "check_cmd":   "helm version --short 2>&1",
        "install": {
            # No sudo on curl/chmod — they write to /tmp.
            # The script itself calls sudo internally for the final move to /usr/local/bin.
            # We run the script as root via sudo bash so it doesn't re-prompt.
            "all": [
                "curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 -o /tmp/get_helm.sh",
                "chmod +x /tmp/get_helm.sh",
                "sudo bash /tmp/get_helm.sh",
            ],
        },
        "verify_cmd": "helm version --short 2>&1",
    },
}


# ==========================================
# 3. JSON Parsing
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
# 4. SSH Execution — sudo via stdin (no TTY needed)
# ==========================================
def execute_ssh(
    client: paramiko.SSHClient,
    command: str,
    sudo_password: str | None = None,
) -> tuple[int, str]:
    """
    Run a command over SSH.

    sudo handling:
    - Commands that start with 'sudo' use 'sudo -S -p ""' so sudo reads
      the password from stdin rather than requiring a TTY.
    - PASSWORDLESS_SUDO=True skips the stdin write entirely.
    - Commands that don't start with 'sudo' run as-is.
    """
    display = command[:100] + ("..." if len(command) > 100 else "")
    print(f"  [SSH] {display}")

    if command.startswith("sudo ") and not PASSWORDLESS_SUDO:
        # -S: read password from stdin
        # -p '': empty prompt string so 'Password:' doesn't pollute our output capture
        sudo_cmd = "sudo -S -p '' " + command[5:]
        stdin, stdout, stderr = client.exec_command(sudo_cmd)
        if sudo_password:
            # Write password to stdin and close — sudo reads one line then proceeds
            stdin.write(sudo_password + "\n")
            stdin.flush()
            stdin.channel.shutdown_write()
    else:
        stdin, stdout, stderr = client.exec_command(command)

    exit_code = stdout.channel.recv_exit_status()
    out = stdout.read().decode("utf-8", errors="replace").strip()
    err = stderr.read().decode("utf-8", errors="replace").strip()

    # Merge stdout + stderr — some tools write version info to stderr
    combined = "\n".join(filter(None, [out, err]))

    if exit_code != 0:
        print(f"  [Exit {exit_code}] {combined[:200]}")
    else:
        print(f"  [OK] {combined[:80]}")

    return exit_code, combined


# ==========================================
# 5. LLM Helper
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


# ==========================================
# 6. SQLite Audit Logging
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
# 7. OS Detection
# ==========================================
def detect_os_flavor(inventory: dict) -> str:
    for item in inventory.get("software", []):
        if item["item"] == "os_name" and item.get("found"):
            name = item["found"].lower()
            if "ubuntu" in name or "debian" in name:
                return "ubuntu"
            if any(k in name for k in ("rhel", "red hat", "centos", "fedora", "rocky", "alma")):
                return "rhel"
    print("  [Warning] Could not detect OS from inventory. Defaulting to ubuntu.")
    return "ubuntu"


# ==========================================
# 8. LLM Retry Loop
# ==========================================
def ask_llm_for_fix(package: str, os_flavor: str, attempt_history: list[dict]) -> dict | None:
    """
    Given the full attempt history for one failing command, ask the LLM
    to diagnose the current error and suggest ONE shell command to fix it.
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
  OS:      {os_flavor}
  Package: {package}
  Attempt: {current['attempt']} of {MAX_RETRIES}

ATTEMPT HISTORY (most recent is the current failure):
{''.join(history_lines)}

RULES:
1. Focus on the MOST RECENT error — not the original one.
2. Do NOT suggest a command already in the attempt history above.
3. Respond with ONLY a raw JSON object — no markdown fences, no preamble.

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
# 9. Single-Package Installer
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
    sudo_password: str | None,
) -> str:
    """
    Install one package end-to-end.
    Returns: "success" | "already_installed" | "failed" | "skipped"
    """
    sep = "─" * 52
    print(f"\n┌{sep}┐")
    print(f"│  📦  {pkg_name}  —  {skill['description']:<40}│")
    print(f"└{sep}┘")

    # Pre-flight: is it already present?
    check_cmd = skill.get("check_cmd", "")
    if check_cmd:
        # Check commands never need sudo — just testing if binary is in PATH
        exit_code, output = execute_ssh(client, check_cmd, sudo_password=None)
        if exit_code == 0:
            print(f"  [Already installed] {output}")
            log_event(conn, session_id, "packages_agent", "check",
                      pkg_name, 0, output, exit_code, "already_installed")
            return "already_installed"

    install_cmds = get_install_commands(skill, os_flavor)
    if not install_cmds:
        print(f"  [Skip] No install commands for OS '{os_flavor}'.")
        return "skipped"

    # ── Run install sequence ──────────────────────────────────────────────
    for step_idx, cmd in enumerate(install_cmds):
        total = len(install_cmds)
        print(f"\n  ── Step {step_idx+1}/{total} ──")
        log_event(conn, session_id, "packages_agent", "command",
                  pkg_name, 1, cmd, None, "running")

        exit_code, output = execute_ssh(client, cmd, sudo_password)
        log_event(conn, session_id, "packages_agent", "command",
                  pkg_name, 1, cmd, exit_code,
                  "success" if exit_code == 0 else "fail")

        if exit_code == 0:
            continue

        # ── Step failed — enter LLM retry loop ───────────────────────────
        print(f"\n  ⚠  Step {step_idx+1} failed. Entering LLM retry loop (max {MAX_RETRIES} attempts)...")

        attempt_history: list[dict] = [{
            "attempt":       1,
            "command":       cmd,
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
                print(f"  [LLM] No response on attempt {attempt_num}.")
                log_event(conn, session_id, "packages_agent", "llm_error",
                          pkg_name, attempt_num, "No LLM response", None, "fail")
                attempt_history.append({
                    "attempt": attempt_num, "command": "(no response)",
                    "exit_code": -1, "output": "", "llm_diagnosis": None, "llm_suggested": None,
                })
                continue

            diagnosis     = llm_result.get("diagnosis", "")
            suggested_cmd = llm_result.get("suggested_command", "").strip()
            confidence    = llm_result.get("confidence", 0.0)

            print(f"  [LLM Diagnosis] {diagnosis}")
            print(f"  [LLM Suggests]  {suggested_cmd}")
            print(f"  [Confidence]    {confidence:.2f}")

            log_event(conn, session_id, "packages_agent", "llm_response",
                      pkg_name, attempt_num, json.dumps(llm_result), None, "retry")

            if not suggested_cmd:
                print("  [LLM] Empty command. Skipping attempt.")
                continue

            attempt_history[-1]["llm_diagnosis"] = diagnosis
            attempt_history[-1]["llm_suggested"] = suggested_cmd

            exit_code, output = execute_ssh(client, suggested_cmd, sudo_password)
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
                print("  Still failing. Continuing retry loop...")

        if not resolved:
            print(f"\n  ✗ All {MAX_RETRIES} attempts exhausted for step {step_idx+1} of '{pkg_name}'.")
            print(f"    Check {DB_PATH} for the full LLM retry history.")
            log_event(conn, session_id, "packages_agent", "result",
                      pkg_name, MAX_RETRIES, "All retries exhausted", None, "exhausted")
            return "failed"

    # ── Verify ───────────────────────────────────────────────────────────
    verify_cmd = skill.get("verify_cmd", "")
    if verify_cmd:
        print(f"\n  [Verify] {verify_cmd}")
        exit_code, output = execute_ssh(client, verify_cmd, sudo_password=None)
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
# 10. Main
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

    # ── Hardware alerts — cannot auto-resolve ─────────────────────────────
    hardware_issues = [
        i for i in inventory.get("hardware", [])
        if i["status"] in ("Missing", "Insufficient")
    ]
    software_todo = [
        i for i in inventory.get("software", [])
        if i["status"] in ("Missing", "Insufficient")
    ]

    if hardware_issues:
        print("\n" + "━" * 54)
        print("  ⚠️   HARDWARE REQUIREMENTS NOT MET")
        print("  These cannot be auto-resolved by this agent.")
        print("━" * 54)
        for item in hardware_issues:
            print(f"  {item['item']:<12} required={item['required']}  "
                  f"found={item['found']}  → {item['status']}")
        print("━" * 54)
        answer = input("\n  Continue with software installation anyway? (y/N): ").strip().lower()
        if answer != "y":
            print("Aborting.")
            return

    if not software_todo:
        print("\n✓ All software requirements already met. Nothing to install.")
        return

    print(f"\nSoftware to install / upgrade: {[i['item'] for i in software_todo]}")

    # ── Credentials ───────────────────────────────────────────────────────
    ssh_password  = getpass.getpass(f"\nEnter SSH password for {SSH_USER}@{SSH_HOST}: ")
    # Same password used for sudo -S unless PASSWORDLESS_SUDO is True
    sudo_password = None if PASSWORDLESS_SUDO else ssh_password

    # ── Setup ─────────────────────────────────────────────────────────────
    conn = init_db()
    session_id = f"pkg_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}"
    open_session(conn, session_id, SSH_HOST)

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    results: dict[str, str] = {}

    try:
        client.connect(hostname=SSH_HOST, port=SSH_PORT,
                       username=SSH_USER, password=ssh_password)

        # Quick root check — if already root, sudo is unnecessary
        _, uid = execute_ssh(client, "id -u", sudo_password=None)
        if uid.strip() == "0":
            print("  [Info] Running as root. sudo calls will succeed without a password.")

        for inv_item in software_todo:
            pkg_name = inv_item["item"]
            skill    = SKILLS.get(pkg_name)

            if not skill:
                print(f"\n  [Skip] No skill defined for '{pkg_name}'. Add it to SKILLS.")
                results[pkg_name] = "no_skill"
                log_event(conn, session_id, "packages_agent", "result",
                          pkg_name, 0, "No skill defined", None, "skipped")
                continue

            result = install_package(
                client, pkg_name, skill, os_flavor,
                conn, session_id, sudo_password
            )
            results[pkg_name] = result
            
            if result in ("success", "already_installed"):
                inv_item["status"] = "Met"
                
        # Save the updated inventory back to file so installer_agent can see the updates
        with open("inventory.json", "w") as f:
            json.dump(inventory, f, indent=4)

    except Exception as e:
        print(f"\nFatal SSH error: {e}")
    finally:
        client.close()
        overall = "failed" if any(r == "failed" for r in results.values()) else "success"
        close_session(conn, session_id, overall)
        conn.close()

    # ── Summary ───────────────────────────────────────────────────────────
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
        print(f"  {icons.get(result,'?')}  {pkg:<18} {result}")

    print()
    failed   = [p for p, r in results.items() if r == "failed"]
    no_skill = [p for p, r in results.items() if r == "no_skill"]

    if failed:
        print(f"  ✗ {len(failed)} package(s) failed after {MAX_RETRIES} retries: {failed}")
        print(f"    Full LLM retry history: {DB_PATH}")
        print("    ⚠  installer_agent.py should NOT run until these are resolved.")
    else:
        print("  ✓ All packages installed. Ready for: python3 installer_agent.py")

    if no_skill:
        print(f"\n  ? Add skills for: {no_skill}")
    print()


if __name__ == "__main__":
    main()
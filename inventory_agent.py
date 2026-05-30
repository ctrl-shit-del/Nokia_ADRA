import json
import re
import paramiko
import getpass
import requests

# ==========================================
# 1. Configuration
# ==========================================
SSH_HOST = "127.0.0.1"
SSH_PORT = 22
SSH_USER = "mystic"

OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL_NAME = "gemma4:31b-cloud"

# Every item in this list MUST have a non-null found value
# before the planner is allowed to declare audit_complete = true.
REQUIRED_ITEMS = [
    "cpu_cores", "ram_gb", "disk_gb", "disk_type",
    "os_name", "python", "docker", "kubernetes", "helm"
]

# Hardcoded fallback: if the LLM keeps failing to check an item,
# we run this command directly without asking the LLM.
FALLBACK_COMMANDS = {
    "cpu_cores":  "nproc",
    "ram_gb":     "free -g | awk '/^Mem:/{print $2}'",
    "disk_gb":    "df -BG / | awk 'NR==2{gsub(\"G\",\"\",$2); print $2}'",
    "disk_type":  "lsblk -d -o name,rota 2>/dev/null | awk 'NR>1{print ($2==0)?\"SSD\":\"HDD\"}' | head -1",
    "os_name":    "grep PRETTY_NAME /etc/os-release | cut -d= -f2 | tr -d '\"'",
    "python":     "python3 --version 2>&1",
    "docker":     "docker --version 2>&1",
    "kubernetes": "kubectl version --client --short 2>&1 | head -1",
    "helm":       "helm version --short 2>&1",
}

# ==========================================
# 2. JSON Parsing — Handles chatty LLM output
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

    # 1. Strip thinking model inner monologue
    text = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()

    # 2. Strip markdown code fences
    text = re.sub(r"```(?:json)?\s*", "", text)
    text = re.sub(r"```", "", text).strip()

    # 3. Extract the first complete {...} block
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"No JSON object found. Raw (first 300 chars):\n{raw[:300]}")

    return json.loads(match.group())


# ==========================================
# 3. Reflexion Memory
# ==========================================
class PlannerMemory:
    """
    Stores good/bad examples from this session and injects them into the
    system prompt so the LLM learns within the run (Reflexion pattern).
    """
    def __init__(self, max_per_bucket: int = 3):
        self.good: list[tuple[str, str]] = []   # (command, what it collected)
        self.bad:  list[tuple[str, str]] = []   # (action_attempted, why it failed)
        self.max = max_per_bucket

    def reward(self, command: str, item: str, value: str):
        entry = (command, f"collected {item} = {value}")
        self.good = (self.good + [entry])[-self.max:]

    def penalise(self, attempted: str, reason: str):
        entry = (attempted, reason)
        self.bad = (self.bad + [entry])[-self.max:]

    def as_prompt_section(self) -> str:
        if not self.good and not self.bad:
            return ""
        lines = []
        if self.good:
            lines.append("EXAMPLES OF CORRECT ACTIONS THIS SESSION (do more of this):")
            for cmd, result in self.good:
                lines.append(f"  GOOD: ran `{cmd}` → {result}")
        if self.bad:
            lines.append("MISTAKES YOU ALREADY MADE (do NOT repeat these):")
            for action, reason in self.bad:
                lines.append(f"  BAD: `{action}` → {reason}")
        return "\n".join(lines)


# ==========================================
# 4. Helper Functions
# ==========================================
def load_requirements():
    try:
        with open("requirements.json", "r") as f:
            return json.load(f)
    except FileNotFoundError:
        print("Error: requirements.json not found. Run requirements_agent.py first.")
        return None


def call_llm(prompt: str) -> str | None:
    """
    Call Ollama. We do NOT pass format='json' here — it causes Gemma/Mistral
    cloud models to sometimes return empty strings. We handle JSON parsing
    ourselves in extract_json() which is more robust.
    """
    payload = {"model": MODEL_NAME, "prompt": prompt, "stream": False}
    try:
        response = requests.post(OLLAMA_URL, json=payload, timeout=120)
        response.raise_for_status()
        return response.json()["response"]
    except requests.exceptions.RequestException as e:
        print(f"  [LLM Error] {e}")
        return None


def execute_ssh(client: paramiko.SSHClient, command: str) -> tuple[int, str]:
    """Run a command over SSH and return (exit_code, combined_output)."""
    print(f"  [SSH] Executing: `{command}`")
    _, stdout, stderr = client.exec_command(command)
    exit_code = stdout.channel.recv_exit_status()
    out = stdout.read().decode("utf-8").strip()
    err = stderr.read().decode("utf-8").strip()
    combined = out if exit_code == 0 else (out + " " + err).strip()
    return exit_code, combined


def format_collected(collected: list[dict]) -> str:
    """Format collected_data list into a readable block for prompts."""
    if not collected:
        return "Nothing collected yet."
    lines = []
    for entry in collected:
        lines.append(f"  item={entry['item']}  cmd=`{entry['command']}`  output={entry['output']!r}")
    return "\n".join(lines)


def items_not_yet_collected(collected: list[dict]) -> list[str]:
    """Return REQUIRED_ITEMS that have no entry in collected_data."""
    covered = {e["item"] for e in collected}
    return [item for item in REQUIRED_ITEMS if item not in covered]


# ==========================================
# 5. Autonomous Planner Loop
# ==========================================
def run_inventory_audit(ssh_client: paramiko.SSHClient, reqs: dict):
    print("\n--- Starting Autonomous LLM Planner Loop ---")

    collected_data: list[dict] = []   # {"item": str, "command": str, "output": str}
    memory = PlannerMemory()
    max_steps = 15
    consecutive_json_failures = 0

    for step in range(max_steps):
        unchecked = items_not_yet_collected(collected_data)

        # All items covered — safe to exit
        if not unchecked:
            print(f"\n  [Guard] All {len(REQUIRED_ITEMS)} items collected. Exiting planner loop.")
            break

        print(f"\n[Step {step+1}/{max_steps}] Unchecked: {unchecked}")

        # After 3 consecutive JSON failures, bypass the LLM for the next
        # unchecked item and run the fallback command directly.
        if consecutive_json_failures >= 3:
            item = unchecked[0]
            cmd = FALLBACK_COMMANDS.get(item)
            if cmd:
                print(f"  [Fallback] LLM unresponsive. Running hardcoded command for '{item}'.")
                consecutive_json_failures = 0
                exit_code, output = execute_ssh(ssh_client, cmd)
                if exit_code == 0 and output:
                    collected_data.append({"item": item, "command": cmd, "output": output})
                    memory.reward(cmd, item, output[:60])
                else:
                    collected_data.append({"item": item, "command": cmd, "output": f"ERROR: {output}"})
                continue

        # Build the planner prompt
        prompt = f"""You are a Linux server auditor. Your job is to collect system information by running shell commands via SSH.

CHECKLIST — you must collect data for ALL of these items before finishing:
{json.dumps(REQUIRED_ITEMS, indent=2)}

ITEMS STILL UNCHECKED (you MUST address at least one of these):
{json.dumps(unchecked, indent=2)}

DATA ALREADY COLLECTED:
{format_collected(collected_data)}

{memory.as_prompt_section()}

RULES:
1. You are NOT done until every item in the CHECKLIST has been collected.
2. You MUST NOT set audit_complete=true while ITEMS STILL UNCHECKED is non-empty.
3. Respond with ONLY a raw JSON object — no markdown, no explanation.
4. Choose one unchecked item and provide the command to check it.

Respond EXACTLY in this format:
{{
  "thought": "I still need to check X. I will run Y to get it.",
  "item": "the_checklist_item_this_command_addresses",
  "command": "exact_bash_command",
  "audit_complete": false
}}"""

        raw = call_llm(prompt)
        if not raw:
            consecutive_json_failures += 1
            memory.penalise("(LLM call)", "LLM returned no response")
            continue

        try:
            action = extract_json(raw)
            consecutive_json_failures = 0
        except (ValueError, json.JSONDecodeError) as e:
            consecutive_json_failures += 1
            memory.penalise("(parse)", f"returned non-JSON: {str(e)[:80]}")
            print(f"  [Error] JSON parse failed ({consecutive_json_failures}/3 before fallback): {e}")
            continue

        print(f"  [LLM Thought]: {action.get('thought', '')}")

        # Guard: LLM tries to exit early
        if action.get("audit_complete") is True:
            still_missing = items_not_yet_collected(collected_data)
            if still_missing:
                memory.penalise(
                    "audit_complete=true",
                    f"tried to finish but {still_missing} were not checked yet"
                )
                print(f"  [Guard] Blocked early exit. Still unchecked: {still_missing}")
                continue
            else:
                print("  [LLM] Declared complete — guard confirms all items covered.")
                break

        command = action.get("command", "").strip()
        item    = action.get("item", "unknown").strip()

        if not command:
            memory.penalise("(empty command)", "returned empty command string")
            print("  [Warning] Empty command returned. Skipping.")
            continue

        exit_code, output = execute_ssh(ssh_client, command)

        if exit_code == 0 and output:
            print(f"  [Output Captured]: {len(output)} characters")
            collected_data.append({"item": item, "command": command, "output": output})
            memory.reward(command, item, output[:60])
        else:
            print(f"  [SSH Error] exit={exit_code}  output={output[:120]}")
            # Still record it so we don't retry the same broken command
            collected_data.append({"item": item, "command": command, "output": f"ERROR(exit={exit_code}): {output}"})
            memory.penalise(command, f"SSH exit code {exit_code}: {output[:80]}")

    # ==========================================
    # 6. Gap Table Generation
    # ==========================================
    print("\n--- Generating Final Gap Table (inventory.json) ---")

    # Separate successful vs failed collections for clarity in the prompt
    good_data = [e for e in collected_data if not e["output"].startswith("ERROR")]
    bad_data  = [e for e in collected_data if e["output"].startswith("ERROR")]

    gap_prompt = f"""You are comparing a Linux server's actual state against deployment requirements.

REQUIREMENTS (from Nokia Aurelis documentation):
{json.dumps(reqs, indent=2)}

SUCCESSFULLY COLLECTED SERVER DATA:
{format_collected(good_data) if good_data else "No data was successfully collected."}

COMMANDS THAT FAILED (set these to null/Missing):
{format_collected(bad_data) if bad_data else "None"}

TASK:
Build the inventory comparison table below.
- For each item, extract the EXACT value from the collected data above. Do not invent values.
- Compare the found value against the requirement.
- status must be exactly one of: "Met", "Insufficient", or "Missing"
  - "Met"           = found value satisfies the requirement
  - "Insufficient"  = found but below the required minimum
  - "Missing"       = no data was collected for this item

IMPORTANT: Do NOT copy the example null values. Fill in real values from the collected data.

Respond with ONLY a raw JSON object — no markdown fences, no explanation:
{{
    "hardware": [
        {{"item": "cpu_cores",  "required": <integer from reqs>,  "found": <integer from data or null>, "status": "<Met|Insufficient|Missing>"}},
        {{"item": "ram_gb",     "required": <integer from reqs>,  "found": <integer from data or null>, "status": "<Met|Insufficient|Missing>"}},
        {{"item": "disk_gb",    "required": <integer from reqs>,  "found": <integer from data or null>, "status": "<Met|Insufficient|Missing>"}},
        {{"item": "disk_type",  "required": <string from reqs>,   "found": <string from data or null>,  "status": "<Met|Insufficient|Missing>"}}
    ],
    "software": [
        {{"item": "python",     "required": <string from reqs>, "found": <string from data or null>, "status": "<Met|Insufficient|Missing>"}},
        {{"item": "docker",     "required": <string from reqs>, "found": <string from data or null>, "status": "<Met|Insufficient|Missing>"}},
        {{"item": "kubernetes", "required": <string from reqs>, "found": <string from data or null>, "status": "<Met|Insufficient|Missing>"}},
        {{"item": "helm",       "required": <string from reqs>, "found": <string from data or null>, "status": "<Met|Insufficient|Missing>"}},
        {{"item": "os_name",    "required": <string from reqs>, "found": <string from data or null>, "status": "<Met|Insufficient|Missing>"}}
    ]
}}"""

    raw_result = call_llm(gap_prompt)

    if raw_result:
        try:
            inv_json = extract_json(raw_result)
            with open("inventory.json", "w") as f:
                json.dump(inv_json, f, indent=4)
            print("Success! Saved to inventory.json")
            print(json.dumps(inv_json, indent=4))
        except (ValueError, json.JSONDecodeError) as e:
            print(f"Failed to parse gap table response: {e}")
            print("Raw response:")
            print(raw_result)
    else:
        print("LLM returned no response for gap table generation.")


# ==========================================
# 7. Main
# ==========================================
def main():
    reqs = load_requirements()
    if not reqs:
        return

    ssh_password = getpass.getpass(prompt=f"Enter SSH password for {SSH_USER}@{SSH_HOST}: ")

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        client.connect(hostname=SSH_HOST, port=SSH_PORT, username=SSH_USER, password=ssh_password)
        run_inventory_audit(client, reqs)
    except Exception as e:
        print(f"Connection/Execution failed: {e}")
    finally:
        client.close()


if __name__ == "__main__":
    main()
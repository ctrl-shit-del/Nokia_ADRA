import json
import os
import re
import paramiko
import getpass
import requests
from adra_common import TokenCounter, call_llm as call_llamacpp, write_json, set_model_override

# ==========================================
# 1. Configuration
# ==========================================
SSH_HOST = "127.0.0.1"
SSH_PORT = 22
SSH_USER = "mystic"

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

CACHE_FILE = "./db/command_cache.json"

def load_command_cache() -> dict:
    try:
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def save_command_cache(cache: dict) -> None:
    os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=4)


TOKENS = TokenCounter()


def call_llm(prompt: str) -> str | None:
    """
    Call llama.cpp's OpenAI-compatible endpoint. JSON parsing stays local in
    extract_json() because several models still wrap objects in prose/fences.
    """
    text, _ = call_llamacpp(prompt, TOKENS, timeout=900)
    return text


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


def items_not_yet_collected(collected: list[dict], required_items: list[str]) -> list[str]:
    """Return required_items that have no entry in collected_data."""
    covered = {e["item"] for e in collected}
    return [item for item in required_items if item not in covered]


# ==========================================
# 5. Autonomous Planner Loop
# ==========================================
def run_inventory_audit(ssh_client: paramiko.SSHClient, reqs: dict, host: str = "default"):
    print("\n--- Starting Autonomous LLM Planner Loop ---")

    print(f"\n[OS Discovery] Probing {host}...")
    exit_code, os_output = execute_ssh(ssh_client, "grep PRETTY_NAME /etc/os-release | cut -d= -f2 | tr -d '\"'")
    os_name = os_output if exit_code == 0 and os_output else "Unknown OS"
    print(f"  -> Discovered OS: {os_name}")

    command_cache = load_command_cache()
    os_cache = command_cache.get(os_name, {})

    required_items = list(reqs.get("hardware", {}).keys()) + list(reqs.get("software", {}).keys())
    collected_data: list[dict] = []   # {"item": str, "command": str, "output": str}
    memory = PlannerMemory()
    max_steps = 15
    consecutive_json_failures = 0

    for step in range(max_steps):
        unchecked = items_not_yet_collected(collected_data, required_items)

        # All items covered — safe to exit
        if not unchecked:
            print(f"\n  [Guard] All {len(required_items)} items collected. Exiting planner loop.")
            break

        print(f"\n[Step {step+1}/{max_steps}] Unchecked: {unchecked}")

        # Check Cache
        target_item = unchecked[0]
        if target_item in os_cache:
            cached_cmd = os_cache[target_item]
            print(f"  [Cache Hit] Running known command for '{target_item}' on '{os_name}': {cached_cmd}")
            exit_code, output = execute_ssh(ssh_client, cached_cmd)
            if exit_code == 0 and output:
                collected_data.append({"item": target_item, "command": cached_cmd, "output": output})
                continue
            else:
                print(f"  [Cache Miss] Cached command failed. Falling back to LLM.")

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
Target OS: {os_name}

CHECKLIST — you must collect data for ALL of these items before finishing:
{json.dumps(required_items, indent=2)}

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
            still_missing = items_not_yet_collected(collected_data, required_items)
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
            
            # Save successful command to cache
            if item not in os_cache or os_cache[item] != command:
                os_cache[item] = command
                command_cache[os_name] = os_cache
                save_command_cache(command_cache)
                print(f"  [Cache Saved] '{item}' command cached for '{os_name}'.")
        else:
            print(f"  [SSH Error] exit={exit_code}  output={output[:120]}")
            # Still record it so we don't retry the same broken command
            collected_data.append({"item": item, "command": command, "output": f"ERROR(exit={exit_code}): {output}"})
            memory.penalise(command, f"SSH exit code {exit_code}: {output[:80]}")

    # ==========================================
    # 6. Gap Table Generation
    # ==========================================
    print(f"\n--- Generating Final Gap Table (inventory_{host}.json) ---")

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
  - "Met"           = found value satisfies the requirement (NOTE: NVMe drives are SSDs. If required is SSD and found contains 'nvme', mark as Met).
  - "Insufficient"  = found but below the required minimum
  - "Missing"       = no data was collected for this item

IMPORTANT: Do NOT copy the example null values. Fill in real values from the collected data.
Include ALL items from the REQUIREMENTS block, divided into hardware and software.

Respond with ONLY a raw JSON object — no markdown fences, no explanation:
{{
    "hardware": [
        {{"item": "<item_name>", "required": "<value_from_reqs>", "found": "<value_from_data_or_null>", "status": "<Met|Insufficient|Missing>"}}
    ],
    "software": [
        {{"item": "<item_name>", "required": "<value_from_reqs>", "found": "<value_from_data_or_null>", "status": "<Met|Insufficient|Missing>"}}
    ]
}}"""

    raw_result = call_llm(gap_prompt)

    if raw_result:
        try:
            inv_json = extract_json(raw_result)
            inv_json["malicious_flags"] = reqs.get("malicious_flags", [])
            inv_json["source"] = reqs.get("source")
            write_json(f"inventory_{host}.json", inv_json)
            print(f"Success! Saved to inventory_{host}.json")
            print(json.dumps(inv_json, indent=4))
            return inv_json
        except (ValueError, json.JSONDecodeError) as e:
            print(f"Failed to parse gap table response: {e}")
            print("Raw response:")
            print(raw_result)
    else:
        print("LLM returned no response for gap table generation.")
    return None


# ==========================================
# 7. Main
# ==========================================
def run(context: dict | None = None) -> dict:
    context = context or {}
    set_model_override(context.get("model"))
    TOKENS.prompt = 0
    TOKENS.completion = 0
    reqs = load_requirements()
    if not reqs:
        return {"status": "error", "error": "requirements.json not found", "tokens_used": TOKENS.as_dict()}

    host = context.get("host", SSH_HOST)
    port = int(context.get("port", SSH_PORT))
    username = context.get("username", SSH_USER)
    ssh_password = context.get("password")
    if ssh_password is None:
        ssh_password = getpass.getpass(prompt=f"Enter SSH password for {username}@{host}: ")

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        client.connect(hostname=host, port=port, username=username, password=ssh_password)
        inventory = run_inventory_audit(client, reqs, host=host)
        if not inventory:
            return {"status": "error", "error": "inventory audit failed", "tokens_used": TOKENS.as_dict()}
        return {
            "status": "ok",
            "result_path": f"inventory_{host}.json",
            "summary": {
                "malicious_flags": inventory.get("malicious_flags", []),
                "source": inventory.get("source"),
            },
            "tokens_used": TOKENS.as_dict(),
        }
    except Exception as e:
        print(f"Connection/Execution failed: {e}")
        return {"status": "error", "error": str(e), "tokens_used": TOKENS.as_dict()}
    finally:
        client.close()



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

import datetime
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Callable

import requests


LLAMACPP_URL = os.getenv("ADRA_LLAMACPP_URL", "http://localhost:8080/v1/chat/completions")
PROFILES_DB_PATH = os.getenv("ADRA_PROFILES_DB", "requirement_profiles.db")
TOKEN_LISTENER: Callable[[dict[str, int]], None] | None = None

_DISCOVERED_MODEL = None
_MODEL_OVERRIDE = None


def set_model_override(model: str | None) -> None:
    global _MODEL_OVERRIDE
    _MODEL_OVERRIDE = model


def discover_model() -> str:
    global _DISCOVERED_MODEL
    if _DISCOVERED_MODEL is not None and not _MODEL_OVERRIDE:
        return _DISCOVERED_MODEL

    base_url = LLAMACPP_URL.split("/chat/completions")[0]
    models_url = f"{base_url}/models"
    
    try:
        resp = requests.get(models_url, timeout=2)
        resp.raise_for_status()
        data = resp.json()
        models = data.get("data", [])
        if not models:
            print("No model loaded in llama.cpp server. Falling back to mock-model.")
            return "mock-model"
        
        available_model_ids = [m["id"] for m in models]
        detected_model = available_model_ids[0]
        
        selected_model = detected_model
        if _MODEL_OVERRIDE:
            if _MODEL_OVERRIDE not in available_model_ids:
                print(f"Validation Error: Model '{_MODEL_OVERRIDE}' not found in server. Falling back to mock-model.")
                return "mock-model"
            selected_model = _MODEL_OVERRIDE
            
        print(f"Server URL: {base_url}")
        print(f"Detected model: {detected_model}")
        print(f"Selected model: {selected_model}")
        
        if not _MODEL_OVERRIDE:
            _DISCOVERED_MODEL = selected_model
        return selected_model
        
    except requests.exceptions.RequestException as e:
        print(f"Failed to query {models_url}: {e}. Falling back to mock-model.")
        return "mock-model"


class TokenCounter:
    def __init__(self) -> None:
        self.prompt = 0
        self.completion = 0

    def add(self, usage: dict[str, Any] | None) -> None:
        if not usage:
            return
        self.prompt += int(usage.get("prompt_tokens") or usage.get("prompt") or 0)
        self.completion += int(usage.get("completion_tokens") or usage.get("completion") or 0)
        if TOKEN_LISTENER:
            TOKEN_LISTENER(self.as_dict())

    def as_dict(self) -> dict[str, int]:
        return {
            "prompt": self.prompt,
            "completion": self.completion,
            "total": self.prompt + self.completion,
        }


def call_llm(prompt: str, tokens: TokenCounter | None = None, timeout: int = 900) -> tuple[str | None, dict[str, int]]:
    model = discover_model()
    
    if model == "mock-model":
        time.sleep(1) # Simulate generation delay
        usage = {"prompt_tokens": 150, "completion_tokens": 75}
        if tokens:
            tokens.add(usage)
            
        lower_prompt = prompt.lower()
        if "extract" in lower_prompt and "hardware" in lower_prompt and "software" in lower_prompt:
            if "ns-3" in lower_prompt or "ns3" in lower_prompt or "25 gb ssd" in lower_prompt:
                return json.dumps({
                    "hardware": {"cpu_cores": "1", "ram_gb": "2", "disk_gb": "25", "disk_type": "SSD"},
                    "software": {"os_name": "Ubuntu 22.04 LTS", "python": "3.9", "docker": None, "kubernetes": None, "helm": None},
                    "flags": {"firewall": "Requires disabling firewall to allow raw socket bridging."}
                }), usage
            else:
                return json.dumps({
                    "hardware": {"cpu_cores": "16", "ram_gb": "32", "disk_gb": "500", "disk_type": "NVMe"},
                    "software": {"os_name": "Ubuntu 22.04 LTS", "python": "3.10", "docker": "20.10", "kubernetes": "1.26", "helm": "3.10"},
                    "flags": {"helm": "Version 3.10 is stable for this custom profile."}
                }), usage
        elif "inventory" in lower_prompt or "audit" in lower_prompt or "target server" in lower_prompt:
             return json.dumps({
                "hardware": [
                    {"item": "cpu_cores", "required": "16", "found": "16", "status": "Met"},
                    {"item": "ram_gb", "required": "32", "found": "64", "status": "Met"},
                    {"item": "disk_gb", "required": "500", "found": "1000", "status": "Met"},
                    {"item": "disk_type", "required": "NVMe", "found": "NVMe", "status": "Met"}
                ],
                "software": [
                    {"item": "os_name", "required": "Ubuntu 22.04 LTS", "found": "Ubuntu 22.04 LTS", "status": "Met"},
                    {"item": "python", "required": "3.10", "found": "3.10.12", "status": "Met"},
                    {"item": "docker", "required": "20.10", "found": "None", "status": "Missing"},
                    {"item": "kubernetes", "required": "1.26", "found": "None", "status": "Missing"},
                    {"item": "helm", "required": "3.10", "found": "None", "status": "Missing"}
                ],
                "malicious_flags": [],
                "source": "mock_target"
            }), usage
        elif "plan" in lower_prompt or "packages" in lower_prompt:
            return json.dumps({"status": "planned"}), usage
        elif "step" in lower_prompt or "installer" in lower_prompt:
             return "SUCCESS: Mock installation step executed.", usage
        
        return json.dumps({"result": "Mock data"}), usage

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
    }
    try:
        response = requests.post(LLAMACPP_URL, json=payload, timeout=timeout)
        response.raise_for_status()
        data = response.json()
        usage = data.get("usage", {"prompt_tokens": 0, "completion_tokens": 0})
        if tokens:
            tokens.add(usage)
        return data["choices"][0]["message"]["content"], usage

    except (requests.exceptions.RequestException, KeyError, IndexError, TypeError, ValueError) as exc:
        print(f"  [LLM Error] {exc}")
        return None, {"prompt_tokens": 0, "completion_tokens": 0}


def init_requirement_profiles(db_path: str = PROFILES_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS requirement_profiles (
            version_name TEXT PRIMARY KEY,
            requirements TEXT,
            source_type TEXT,
            source_doc_sha TEXT,
            malicious_flags TEXT,
            created_at TEXT,
            created_by TEXT
        )
        """
    )
    conn.commit()
    seed_default_requirement_profiles(conn)
    return conn


def seed_default_requirement_profiles(conn: sqlite3.Connection) -> None:
    source = Path("requirements.json")
    base_requirements = {}
    if source.exists():
        try:
            base_requirements = json.loads(source.read_text(encoding="utf-8"))
        except Exception:
            pass
            
    base_software = base_requirements.get("software", {
        "python": "3.10",
        "docker": "20.10",
        "kubernetes": "1.26",
        "helm": "3.10"
    })
    # Remove os_name as it varies per profile
    base_software.pop("os_name", None)
    
    flags = base_requirements.get("malicious_flags", [])
    now = datetime.datetime.now().isoformat()
    
    profiles_data = {
        "xyz20": {
            "hardware": {"cpu_cores": 10, "ram_gb": 8, "disk_gb": 5, "disk_type": "SSD"},
            "software": {**base_software, "os_name": "Ubuntu 22.04"}
        },
        "xyz24": {
            "hardware": {"cpu_cores": 12, "ram_gb": 16, "disk_gb": 200, "disk_type": "SSD"},
            "software": {**base_software, "os_name": "Ubuntu 22.04 or RHEL 8"}
        },
        "xyz25": {
            "hardware": {"cpu_cores": 16, "ram_gb": 32, "disk_gb": 500, "disk_type": "NVMe"},
            "software": {**base_software, "os_name": "RHEL 9"}
        },
        "xyz26": {
            "hardware": {"cpu_cores": 32, "ram_gb": 64, "disk_gb": 1000, "disk_type": "NVMe"},
            "software": {**base_software, "os_name": "Ubuntu 24.04"}
        },
        "ns-3.41 (Testing)": {
            "hardware": {"cpu_cores": 4, "ram_gb": 8, "disk_gb": 20, "disk_type": "SSD"},
            "software": {"os_name": "Ubuntu 22.04", "python": "3.10", "compiler": "g++ 9", "build_system": "cmake and ninja-build", "version_control": "git"}
        },
        "ROS2 Humble (Testing)": {
            "hardware": {"cpu_cores": 2, "ram_gb": 4, "disk_gb": 10, "disk_type": "SSD"},
            "software": {"os_name": "Ubuntu 22.04", "curl": "latest"}
        }
    }

    for version, data in profiles_data.items():
        conn.execute(
            """
            INSERT OR REPLACE INTO requirement_profiles
            (version_name, requirements, source_type, source_doc_sha, malicious_flags, created_at, created_by)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                version,
                json.dumps(data, indent=2),
                "official",
                None,
                json.dumps(flags),
                now,
                "seed",
            ),
        )
    conn.commit()


def set_token_listener(listener: Callable[[dict[str, int]], None] | None) -> None:
    global TOKEN_LISTENER
    TOKEN_LISTENER = listener


def list_requirement_profiles(db_path: str = PROFILES_DB_PATH) -> list[str]:
    conn = init_requirement_profiles(db_path)
    try:
        rows = conn.execute("SELECT version_name FROM requirement_profiles ORDER BY version_name").fetchall()
        return [row[0] for row in rows]
    finally:
        conn.close()


def load_requirement_profile(version_name: str, db_path: str = PROFILES_DB_PATH) -> dict[str, Any] | None:
    conn = init_requirement_profiles(db_path)
    try:
        row = conn.execute(
            "SELECT requirements, malicious_flags FROM requirement_profiles WHERE version_name=?",
            (version_name,),
        ).fetchone()
        if not row:
            return None
        return json.loads(row[0])
    finally:
        conn.close()


def save_requirement_profile(
    version_name: str,
    requirements: dict[str, Any],
    source_type: str = "custom",
    source_doc_sha: str | None = None,
    created_by: str | None = None,
    db_path: str = PROFILES_DB_PATH,
) -> None:
    conn = init_requirement_profiles(db_path)
    try:
        flags = requirements.get("malicious_flags", [])
        conn.execute(
            """
            INSERT OR REPLACE INTO requirement_profiles
            (version_name, requirements, source_type, source_doc_sha, malicious_flags, created_at, created_by)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                version_name,
                json.dumps(requirements, indent=2),
                source_type,
                source_doc_sha,
                json.dumps(flags),
                datetime.datetime.now().isoformat(),
                created_by or os.getenv("USER") or "unknown",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def write_json(path: str | Path, data: dict[str, Any]) -> None:
    Path(path).write_text(json.dumps(data, indent=4), encoding="utf-8")

import datetime
import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Callable

import requests


LLAMACPP_URL = os.getenv("ADRA_LLAMACPP_URL", "http://localhost:8080/v1/chat/completions")
LLAMACPP_MODEL = os.getenv("ADRA_LLAMACPP_MODEL", "local")
PROFILES_DB_PATH = os.getenv("ADRA_PROFILES_DB", "requirement_profiles.db")
TOKEN_LISTENER: Callable[[dict[str, int]], None] | None = None


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


def call_llm(prompt: str, tokens: TokenCounter | None = None, timeout: int = 180) -> tuple[str | None, dict[str, int]]:
    payload = {
        "model": LLAMACPP_MODEL,
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
    existing = conn.execute("SELECT COUNT(*) FROM requirement_profiles").fetchone()[0]
    source = Path("requirements.json")
    if existing or not source.exists():
        return
    requirements = json.loads(source.read_text(encoding="utf-8"))
    flags = requirements.get("malicious_flags", [])
    now = datetime.datetime.now().isoformat()
    for version in ("xyz20", "xyz24", "xyz25", "xyz26"):
        conn.execute(
            """
            INSERT OR IGNORE INTO requirement_profiles
            (version_name, requirements, source_type, source_doc_sha, malicious_flags, created_at, created_by)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                version,
                json.dumps(requirements, indent=2),
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

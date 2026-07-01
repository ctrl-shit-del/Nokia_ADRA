import os
import json
import re
import hashlib
import argparse
from adra_common import (
    TokenCounter,
    call_llm as call_llamacpp,
    list_requirement_profiles,
    load_requirement_profile,
    save_requirement_profile,
    write_json,
    set_model_override,
)

# ==========================================
# 1. Configuration
# ==========================================
EMBEDDING_MODEL = "all-MiniLM-L6-v2"
DB_PATH        = "./db/chroma_db"
DOCS_DIR       = "./docs"

embedder = None
collection = None


def get_vector_collection():
    global embedder, collection
    if embedder is None or collection is None:
        import chromadb
        from sentence_transformers import SentenceTransformer

        embedder = SentenceTransformer(EMBEDDING_MODEL)
        chroma_client = chromadb.PersistentClient(path=DB_PATH)
        collection = chroma_client.get_or_create_collection(name="aurelis_docs")
    return embedder, collection

# ==========================================
# 2. JSON Parsing — same helper as inventory_agent
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
        raise ValueError(f"No JSON object found. Raw (first 300 chars):\n{raw[:300]}")

    return json.loads(match.group())


# ==========================================
# 3. LLM Communication
# ==========================================
TOKENS = TokenCounter()


def call_llm(prompt: str) -> str | None:
    """
    Call llama.cpp's OpenAI-compatible endpoint and parse the response ourselves,
    which handles fence-wrapping and thinking models more reliably.
    """
    text, _ = call_llamacpp(prompt, TOKENS, timeout=900)
    return text


# ==========================================
# 4. Document Ingestion
# ==========================================
def ingest_documents():
    """Read .txt files from docs/, chunk by paragraph, store in ChromaDB."""
    embedder, collection = get_vector_collection()
    documents, metadatas, ids = [], [], []

    for filename in os.listdir(DOCS_DIR):
        if filename.endswith(".txt"):
            filepath = os.path.join(DOCS_DIR, filename)
            with open(filepath, "r") as f:
                chunks = [c.strip() for c in f.read().split("\n\n") if c.strip()]
                for i, chunk in enumerate(chunks):
                    documents.append(chunk)
                    metadatas.append({"source": filename, "chunk_index": i})
                    ids.append(f"{filename}_chunk_{i}")

    if documents:
        collection.upsert(
            documents=documents,
            embeddings=embedder.encode(documents).tolist(),
            metadatas=metadatas,
            ids=ids,
        )
        print(f"Ingested {len(documents)} chunks from {DOCS_DIR}/")
    else:
        print(f"Warning: No .txt files found in {DOCS_DIR}/")


def extract_document_text(filepath: str, raw: bytes) -> str:
    suffix = os.path.splitext(filepath)[1].lower()
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise RuntimeError("PDF upload support requires pypdf. Install requirements.txt.") from exc
        reader = PdfReader(filepath)
        return "\n\n".join(page.extract_text() or "" for page in reader.pages)
    return raw.decode("utf-8", errors="replace")


def ingest_text_document(filepath: str) -> str:
    """Read one text/html/pdf document, chunk it, and store chunks in ChromaDB."""
    embedder, collection = get_vector_collection()
    with open(filepath, "rb") as f:
        raw = f.read()
    digest = hashlib.sha256(raw).hexdigest()
    text = extract_document_text(filepath, raw)
    filename = os.path.basename(filepath)
    chunks = [c.strip() for c in re.split(r"\n\s*\n|(?<=</p>)", text) if c.strip()]
    documents, metadatas, ids = [], [], []
    for i, chunk in enumerate(chunks):
        documents.append(chunk)
        metadatas.append({"source": filename, "chunk_index": i, "sha256": digest})
        ids.append(f"{digest[:8]}_{filename}_chunk_{i}")
    if documents:
        collection.upsert(
            documents=documents,
            embeddings=embedder.encode(documents).tolist(),
            metadatas=metadatas,
            ids=ids,
        )
    return digest


# ==========================================
# 5. Targeted RAG Queries
# ==========================================
def retrieve_context(queries: list[str], n_results: int = 5, where_filter: dict | None = None) -> str:
    """
    Run multiple targeted queries and return deduplicated context.
    Using multiple focused queries catches requirements that a single
    broad query often misses.
    """
    embedder, collection = get_vector_collection()
    seen_ids = set()
    all_docs = []

    for query in queries:
        kwargs = {
            "query_embeddings": embedder.encode([query]).tolist(),
            "n_results": n_results,
        }
        if where_filter:
            kwargs["where"] = where_filter
            
        results = collection.query(**kwargs)
        for doc, meta, doc_id in zip(
            results["documents"][0],
            results["metadatas"][0],
            results["ids"][0],
        ):
            if doc_id not in seen_ids:
                seen_ids.add(doc_id)
                all_docs.append(f"[source: {meta['source']}]\n{doc}")

    return "\n\n---\n\n".join(all_docs)


# ==========================================
# 6. Requirements Extraction
# ==========================================
def heuristic_malicious_flags(context: str) -> list[dict]:
    flags: list[dict] = []
    patterns = [
        (
            r"curl\s+http://\d{1,3}(?:\.\d{1,3}){3}/[^\s|;]+[^|\n;]*\|\s*sudo\s+bash",
            "Installation command pipes an unsigned script from a raw IP address over HTTP into sudo bash",
            "high",
        ),
        (
            r"wget\s+http://\d{1,3}(?:\.\d{1,3}){3}/[^\s|;]+[^|\n;]*\|\s*sudo\s+bash",
            "Installation command pipes an unsigned script from a raw IP address over HTTP into sudo bash",
            "high",
        ),
        (
            r"\b(setenforce\s+0|systemctl\s+stop\s+firewalld|iptables\s+-F)\b",
            "Requirement disables or flushes host security controls",
            "medium",
        ),
    ]
    for pattern, reason, severity in patterns:
        for match in re.finditer(pattern, context, flags=re.IGNORECASE):
            excerpt = context[max(0, match.start() - 80):match.end() + 80].replace("\n", " ")
            flags.append({
                "item": match.group(0).strip()[:80],
                "reason": reason,
                "severity": severity,
                "source_excerpt_ref": excerpt,
            })
    return flags


def scan_malicious_flags(req_json: dict, context: str) -> list[dict]:
    flags = heuristic_malicious_flags(context)
    prompt = f"""You are reviewing deployment requirements for suspicious software or commands.

REQUIREMENTS JSON:
{json.dumps(req_json, indent=2)}

SOURCE CONTEXT:
{context[:12000]}

Flag only concrete red flags such as unsigned downloads from raw IPs over HTTP,
pastebin-style installers, commands disabling firewalls/SELinux, or packages from
non-standard malicious-looking repositories.

Respond with ONLY raw JSON:
{{"malicious_flags":[{{"item":"...","reason":"...","severity":"low|medium|high","source_excerpt_ref":"..."}}]}}
If nothing is suspicious, return {{"malicious_flags":[]}}."""
    raw = call_llm(prompt)
    if not raw:
        return flags
    try:
        result = extract_json(raw)
        return flags + result.get("malicious_flags", [])
    except (ValueError, json.JSONDecodeError):
        return flags


def extract_requirements(source: str | None = None, source_doc_sha: str | None = None) -> dict | None:
    print("\n--- Starting Requirements Extraction ---")

    # Multiple targeted queries — each one retrieves different relevant chunks.
    # One broad query often misses specific version numbers.
    queries = [
        "minimum CPU cores processor requirements",
        "minimum RAM memory gigabytes requirement",
        "disk storage SSD HDD space requirement gigabytes",
        "supported operating system Ubuntu RHEL version",
        "Python version requirement minimum",
        "Docker version requirement minimum",
        "Kubernetes kubectl version requirement minimum",
        "Helm version requirement minimum",
    ]

    print(f"Running {len(queries)} targeted queries against the vector DB...")
    where_filter = {"sha256": source_doc_sha} if source_doc_sha else None
    context = retrieve_context(queries, n_results=5, where_filter=where_filter)
    print(f"Retrieved {len(context)} characters of context.\n")

    # NOTE: Field names here match EXACTLY what inventory_agent.py expects.
    # If you change these names here, update REQUIRED_ITEMS in inventory_agent.py.
    prompt = f"""You are an expert system administrator extracting deployment requirements from documentation.

DOCUMENTATION CONTEXT:
{context}

Extract the hardware and software requirements from the context above.

RULES:
1. Use ONLY information found in the DOCUMENTATION CONTEXT above.
2. If a value is not mentioned in the context, use null.
3. Version strings must be in "major.minor" format (e.g. "3.10", "20.10", "1.26").
4. Respond with ONLY a raw JSON object — no markdown fences, no explanation.

Output EXACTLY this structure (field names must match exactly):
{{
    "hardware": {{
        "cpu_cores":  <integer minimum cores, or null if not found>,
        "ram_gb":     <integer minimum RAM in GB, or null if not found>,
        "disk_gb":    <integer minimum disk in GB, or null if not found>,
        "disk_type":  "<SSD or HDD, or null if not found>"
    }},
    "software": {{
        "os_name":    "<supported OS name/version, or null if not found>",
        "python":     "<minimum version string, or null if not found>",
        "<any_other_software_name_found_in_doc>": "<minimum version string, or null if not found>"
    }}
}}"""

    print("Sending context to LLM for extraction...")
    raw = call_llm(prompt)

    if not raw:
        print("LLM returned no response.")
        return None

    try:
        req_json = extract_json(raw)
    except (ValueError, json.JSONDecodeError) as e:
        print(f"Failed to parse response: {e}")
        print("Raw response:")
        print(raw)
        return None

    # Basic validation — warn if any expected fields are null
    missing_fields = []
    for section in ("hardware", "software"):
        for key, val in req_json.get(section, {}).items():
            if val is None:
                missing_fields.append(f"{section}.{key}")

    if missing_fields:
        print(f"Warning: The following fields were not found in the docs: {missing_fields}")
        print("Check that your docs/ folder contains the relevant sections.")

    req_json["malicious_flags"] = scan_malicious_flags(req_json, context)
    if source:
        req_json["source"] = source

    # Strip null-valued fields so the inventory agent only checks items
    # that were actually specified in the uploaded document
    for section in ("hardware", "software"):
        if section in req_json:
            req_json[section] = {k: v for k, v in req_json[section].items() if v is not None}

    write_json("requirements.json", req_json)

    print("\nSuccess! Saved to requirements.json:")
    print(json.dumps(req_json, indent=4))
    return req_json


def run(context: dict | None = None) -> dict:
    context = context or {}
    set_model_override(context.get("model"))
    mode = context.get("mode", "docs")
    TOKENS.prompt = 0
    TOKENS.completion = 0

    if mode in ("dropdown", "profile", "known"):
        version = context.get("version")
        if not version:
            return {"status": "error", "error": "version is required", "tokens_used": TOKENS.as_dict()}
        profile = load_requirement_profile(version)
        if not profile:
            return {
                "status": "error",
                "error": f"requirement profile not found: {version}",
                "available_profiles": list_requirement_profiles(),
                "tokens_used": TOKENS.as_dict(),
            }
        
        profile["source"] = f"profile:{version}"
        write_json("requirements.json", profile)
        return {
            "status": "ok",
            "result_path": "requirements.json",
            "summary": {
                "source": f"profile:{version}",
                "malicious_flags": profile.get("malicious_flags", []),
            },
            "tokens_used": TOKENS.as_dict(),
        }

    source_doc_sha = None
    if context.get("document_path"):
        source_doc_sha = ingest_text_document(context["document_path"])
        # IMPORTANT: store the FULL sha256, not a truncated prefix.
        # installer_agent.py later re-derives doc_sha from this string to filter
        # ChromaDB by exact metadata match — truncating it here breaks that match
        # (ChromaDB 'where' filters are exact-equality, not prefix), causing
        # installer_agent to silently retrieve zero context chunks and fall back
        # to a fully hallucinated install pipeline (e.g. example.com placeholder URLs).
        source = f"upload:{os.path.basename(context['document_path'])}:{source_doc_sha}"
    else:
        ingest_documents()
        source = "docs"

    req_json = extract_requirements(source=source, source_doc_sha=source_doc_sha)
    if not req_json:
        return {"status": "error", "error": "requirements extraction failed", "tokens_used": TOKENS.as_dict()}

    if context.get("save_profile") and context.get("version"):
        save_requirement_profile(
            context["version"],
            req_json,
            source_type="custom",
            source_doc_sha=source_doc_sha,
            created_by=context.get("created_by"),
        )

    return {
        "status": "ok",
        "result_path": "requirements.json",
        "summary": {
            "source": req_json.get("source"),
            "malicious_flags": req_json.get("malicious_flags", []),
        },
        "tokens_used": TOKENS.as_dict(),
    }


def parse_args() -> dict:
    parser = argparse.ArgumentParser(description="ADRA Requirements Agent")
    parser.add_argument("--mode", default="docs", choices=["docs", "profile", "dropdown", "known", "upload"])
    parser.add_argument("--version")
    parser.add_argument("--document-path")
    parser.add_argument("--save-profile", action="store_true")
    parser.add_argument("--created-by")
    parser.add_argument("--model", help="Override the automatically discovered llama.cpp model")
    args = parser.parse_args()
    return {
        "mode": "profile" if args.mode in ("dropdown", "known") else args.mode,
        "version": args.version,
        "document_path": args.document_path,
        "save_profile": args.save_profile,
        "created_by": args.created_by,
        "model": args.model,
    }


# ==========================================
# 7. Main
# ==========================================
if __name__ == "__main__":
    print(json.dumps(run(parse_args()), indent=2))

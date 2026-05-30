import os
import json
import re
import requests
import chromadb
from sentence_transformers import SentenceTransformer

# ==========================================
# 1. Configuration
# ==========================================
OLLAMA_URL     = "http://localhost:11434/api/generate"
MODEL_NAME     = "gemma4:31b-cloud"
EMBEDDING_MODEL = "all-MiniLM-L6-v2"
DB_PATH        = "./db/chroma_db"
DOCS_DIR       = "./docs"

embedder       = SentenceTransformer(EMBEDDING_MODEL)
chroma_client  = chromadb.PersistentClient(path=DB_PATH)
collection     = chroma_client.get_or_create_collection(name="aurelis_docs")

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
def call_llm(prompt: str) -> str | None:
    """
    Call Ollama without format='json' — we parse the response ourselves
    which handles fence-wrapping and thinking models more reliably.
    """
    payload = {"model": MODEL_NAME, "prompt": prompt, "stream": False}
    try:
        response = requests.post(OLLAMA_URL, json=payload, timeout=120)
        response.raise_for_status()
        return response.json()["response"]
    except requests.exceptions.RequestException as e:
        print(f"LLM Connection Error: {e}")
        return None


# ==========================================
# 4. Document Ingestion
# ==========================================
def ingest_documents():
    """Read .txt files from docs/, chunk by paragraph, store in ChromaDB."""
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


# ==========================================
# 5. Targeted RAG Queries
# ==========================================
def retrieve_context(queries: list[str], n_results: int = 5) -> str:
    """
    Run multiple targeted queries and return deduplicated context.
    Using multiple focused queries catches requirements that a single
    broad query often misses.
    """
    seen_ids = set()
    all_docs = []

    for query in queries:
        results = collection.query(
            query_embeddings=embedder.encode([query]).tolist(),
            n_results=n_results,
        )
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
def extract_requirements():
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
    context = retrieve_context(queries, n_results=5)
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
        "cpu_cores":  <integer minimum cores, e.g. 12>,
        "ram_gb":     <integer minimum RAM in GB, e.g. 32>,
        "disk_gb":    <integer minimum disk in GB, e.g. 200>,
        "disk_type":  "<SSD or HDD>"
    }},
    "software": {{
        "os_name":    "<supported OS, e.g. Ubuntu 22.04 or RHEL 8>",
        "python":     "<minimum version string, e.g. 3.10>",
        "docker":     "<minimum version string, e.g. 20.10>",
        "kubernetes": "<minimum version string, e.g. 1.26>",
        "helm":       "<minimum version string, e.g. 3.10>"
    }}
}}"""

    print("Sending context to LLM for extraction...")
    raw = call_llm(prompt)

    if not raw:
        print("LLM returned no response.")
        return

    try:
        req_json = extract_json(raw)
    except (ValueError, json.JSONDecodeError) as e:
        print(f"Failed to parse response: {e}")
        print("Raw response:")
        print(raw)
        return

    # Basic validation — warn if any expected fields are null
    missing_fields = []
    for section in ("hardware", "software"):
        for key, val in req_json.get(section, {}).items():
            if val is None:
                missing_fields.append(f"{section}.{key}")

    if missing_fields:
        print(f"Warning: The following fields were not found in the docs: {missing_fields}")
        print("Check that your docs/ folder contains the relevant sections.")

    with open("requirements.json", "w") as f:
        json.dump(req_json, f, indent=4)

    print("\nSuccess! Saved to requirements.json:")
    print(json.dumps(req_json, indent=4))


# ==========================================
# 7. Main
# ==========================================
if __name__ == "__main__":
    ingest_documents()
    extract_requirements()
"""Runtime configuration, read once from the environment.

Two provider modes:

    LLM_PROVIDER=ollama   local models, nothing leaves the machine (dev default)
    LLM_PROVIDER=mistral  hosted API, required for any free-tier deployment
                          because a 7B model cannot be hosted for free

Model names are all env-overridable. Providers rename models fairly often, so
nothing here is hardcoded in a way that requires a code change to fix.
"""

from __future__ import annotations

import os
from pathlib import Path

# Load .env if present. Real environment variables always win, so a platform's
# injected config (Cloud Run, Vercel, etc.) is never overridden by a stray file.
try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)
except ImportError:  # python-dotenv is optional
    pass

# --------------------------------------------------------------------------- provider

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "ollama").strip().lower()
if LLM_PROVIDER not in {"ollama", "mistral"}:
    raise ValueError(f"LLM_PROVIDER must be 'ollama' or 'mistral', got {LLM_PROVIDER!r}")

# --------------------------------------------------------------------------- ollama

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")

# --------------------------------------------------------------------------- mistral

MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY", "").strip()

# --------------------------------------------------------------------------- models
#
# Three distinct jobs, three separate settings. They have genuinely different
# requirements, and on a paid API routing them to one model wastes money:
#
#   classifier — small JSON classification, runs on every question
#   sql        — the hard part; worth the strongest model available
#   narrative  — prose over numbers that have already been computed
#
_DEFAULTS = {
    "ollama": {
        "classifier": "llama3.2:latest",
        "sql": "qwen2.5-coder:7b",
        "narrative": "llama3.2:latest",
        "embed": "nomic-embed-text",
    },
    "mistral": {
        "classifier": "mistral-small-latest",
        "sql": "codestral-latest",
        "narrative": "mistral-small-latest",
        "embed": "mistral-embed",
    },
}

_d = _DEFAULTS[LLM_PROVIDER]

# LLM_MODEL, if set, overrides all three chat roles at once — handy for local
# testing when you only have one model pulled.
_ALL_ROLES_OVERRIDE = os.getenv("LLM_MODEL", "").strip()

CLASSIFIER_MODEL = os.getenv("CLASSIFIER_MODEL", "").strip() or _ALL_ROLES_OVERRIDE or _d["classifier"]
SQL_MODEL = os.getenv("SQL_MODEL", "").strip() or _ALL_ROLES_OVERRIDE or _d["sql"]
NARRATIVE_MODEL = os.getenv("NARRATIVE_MODEL", "").strip() or _ALL_ROLES_OVERRIDE or _d["narrative"]
EMBED_MODEL = os.getenv("EMBED_MODEL", "").strip() or _d["embed"]

# --------------------------------------------------------------------------- embeddings
#
# Embedding dimensions differ per model (nomic-embed-text is 768, mistral-embed
# is 1024). Vectors from different models cannot share a collection, so the
# provider is recorded in the collection name — switching providers starts a
# clean index instead of silently corrupting the old one.
EMBED_NAMESPACE = f"{LLM_PROVIDER}_{EMBED_MODEL}".replace(":", "_").replace("/", "_")

# Hosted APIs rate-limit per minute, and a wide workbook can have hundreds of
# columns. Send them in chunks rather than one giant request.
EMBED_BATCH_SIZE = int(os.getenv("EMBED_BATCH_SIZE", "32"))
API_MAX_RETRIES = int(os.getenv("API_MAX_RETRIES", "4"))
API_BACKOFF_SECONDS = float(os.getenv("API_BACKOFF_SECONDS", "1.5"))

# --------------------------------------------------------------------------- storage

DATA_DIR = Path(os.getenv("DATA_DIR", "./data")).resolve()
DB_DIR = DATA_DIR / "duckdb"
CHROMA_DIR = DATA_DIR / "chroma"

# MotherDuck is hosted DuckDB. Setting a token switches every workbook from a
# local .duckdb file to a hosted database — same SQL dialect, so nothing in the
# prompts or in sqlguard changes. This is what makes the service deployable to
# a host whose filesystem is wiped on restart.
MOTHERDUCK_TOKEN = os.getenv("MOTHERDUCK_TOKEN", "").strip()
USE_MOTHERDUCK = bool(MOTHERDUCK_TOKEN)

# Chroma Cloud — same client API as the local persistent store, so switching is
# a connection change rather than a rewrite of the retrieval code.
CHROMA_API_KEY = os.getenv("CHROMA_API_KEY", "").strip()
CHROMA_TENANT = os.getenv("CHROMA_TENANT", "").strip()
CHROMA_DATABASE = os.getenv("CHROMA_DATABASE", "sheetsense").strip()
USE_CHROMA_CLOUD = bool(CHROMA_API_KEY and CHROMA_TENANT)

# Chroma posts anonymised usage events to its own servers by default. Off —
# an app positioned as local-by-default should not make undeclared outbound
# calls, and it is noise in the logs either way.
os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")

for directory in (DB_DIR, CHROMA_DIR):
    directory.mkdir(parents=True, exist_ok=True)

# --------------------------------------------------------------------------- limits

CORS_ORIGINS = [
    origin.strip()
    for origin in os.getenv(
        "CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173,http://localhost:3000"
    ).split(",")
    if origin.strip()
]

MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(50 * 1024 * 1024)))
# Aggregates are computed over the whole table in DuckDB; only the returned
# result set is capped, so a stray SELECT * cannot flood the context window.
MAX_RESULT_ROWS = int(os.getenv("MAX_RESULT_ROWS", "200"))


def describe() -> dict[str, str | int]:
    """Non-secret config summary, surfaced on /api/health for debugging deploys."""
    return {
        "provider": LLM_PROVIDER,
        "classifier_model": CLASSIFIER_MODEL,
        "sql_model": SQL_MODEL,
        "narrative_model": NARRATIVE_MODEL,
        "embed_model": EMBED_MODEL,
        "embed_namespace": EMBED_NAMESPACE,
        "embed_batch_size": EMBED_BATCH_SIZE,
    }

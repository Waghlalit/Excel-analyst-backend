"""The vector layer.

Four collections, each doing a different job. They are separate rather than one
mixed index because the document shapes differ and each needs its own `k`:

  columns   — one doc per column; finds *where* to look in a wide workbook
  glossary  — business term -> SQL fragment; resolves ambiguous vocabulary
  recipes   — past question -> working SQL; retrieved as few-shot examples
  insights  — findings from earlier turns; lets follow-ups build on context

Every query is filtered by `workbook_id` so one upload can never leak into
another's answers.
"""

from __future__ import annotations

from typing import Any

import chromadb

from . import config
from .config import CHROMA_DIR, EMBED_NAMESPACE
from .llm import embed


def _build_client():
    """Chroma Cloud when credentials are present, otherwise a local store.

    Both expose the same collection API, so nothing below this line changes.
    """
    if config.USE_CHROMA_CLOUD:
        return chromadb.CloudClient(
            api_key=config.CHROMA_API_KEY,
            tenant=config.CHROMA_TENANT,
            database=config.CHROMA_DATABASE,
        )
    return chromadb.PersistentClient(
        path=str(CHROMA_DIR),
        settings=chromadb.config.Settings(anonymized_telemetry=False),
    )


_client = _build_client()

COLLECTIONS = ("columns", "glossary", "recipes", "insights")


def _collection(name: str):
    # The embedding model is part of the collection name. Vectors from
    # different models have different dimensions and are not comparable, so
    # switching provider starts a clean index rather than silently poisoning
    # the existing one with incompatible vectors.
    return _client.get_or_create_collection(
        name=f"{name}__{EMBED_NAMESPACE}", metadata={"hnsw:space": "cosine"}
    )


def add(name: str, ids: list[str], documents: list[str], metadatas: list[dict[str, Any]]) -> None:
    if not ids:
        return
    _collection(name).add(
        ids=ids,
        documents=documents,
        embeddings=embed(documents),
        metadatas=metadatas,
    )


def query(
    name: str, text: str, workbook_id: str, k: int = 8
) -> list[tuple[str, dict[str, Any], float]]:
    """Return (document, metadata, similarity) triples, best first."""
    collection = _collection(name)
    if collection.count() == 0:
        return []

    result = collection.query(
        query_embeddings=embed([text]),
        n_results=k,
        where={"workbook_id": workbook_id},
    )

    documents = result.get("documents") or [[]]
    metadatas = result.get("metadatas") or [[]]
    distances = result.get("distances") or [[]]

    triples: list[tuple[str, dict[str, Any], float]] = []
    for document, metadata, distance in zip(documents[0], metadatas[0], distances[0]):
        # Chroma returns cosine distance; convert to a 0..1 similarity.
        triples.append((document, dict(metadata), max(0.0, 1.0 - float(distance))))
    return triples


def forget_workbook(workbook_id: str) -> None:
    """Drop everything belonging to a workbook (used when it is re-uploaded)."""
    for name in COLLECTIONS:
        collection = _collection(name)
        try:
            collection.delete(where={"workbook_id": workbook_id})
        except Exception:
            # An empty collection raises rather than no-opping in some versions.
            pass


def _glossary_id(workbook_id: str, term: str) -> str:
    return f"{workbook_id}::term::{term.strip().lower()}"


def add_glossary(workbook_id: str, term: str, definition: str, sql: str = "") -> dict[str, Any]:
    """Teach the system one business term.

    This is what stops "revenue" meaning something different in every answer.
    The document is prose (so a question can match it semantically); the SQL
    fragment rides along in metadata and is injected verbatim into the
    SQL-writing prompt.
    """
    term = term.strip()
    if not term:
        raise ValueError("A glossary term cannot be empty.")

    document = f"Term: {term}\nDefinition: {definition.strip()}"
    if sql.strip():
        document += f"\nComputed as: {sql.strip()}"

    entry_id = _glossary_id(workbook_id, term)
    collection = _collection("glossary")
    # Upsert semantics: re-adding a term replaces it rather than duplicating.
    try:
        collection.delete(ids=[entry_id])
    except Exception:
        pass

    collection.add(
        ids=[entry_id],
        documents=[document],
        embeddings=embed([document]),
        metadatas=[
            {
                "workbook_id": workbook_id,
                "term": term,
                "definition": definition.strip(),
                "sql": sql.strip(),
            }
        ],
    )
    return {"term": term, "definition": definition.strip(), "sql": sql.strip()}


def list_glossary(workbook_id: str) -> list[dict[str, Any]]:
    collection = _collection("glossary")
    if collection.count() == 0:
        return []
    result = collection.get(where={"workbook_id": workbook_id})
    entries = [
        {
            "term": metadata.get("term", ""),
            "definition": metadata.get("definition", ""),
            "sql": metadata.get("sql", ""),
        }
        for metadata in (result.get("metadatas") or [])
    ]
    return sorted(entries, key=lambda entry: entry["term"].lower())


def delete_glossary(workbook_id: str, term: str) -> None:
    _collection("glossary").delete(ids=[_glossary_id(workbook_id, term)])


def remember_recipe(workbook_id: str, question: str, sql: str) -> None:
    """Store a question whose SQL ran successfully, for future few-shot reuse."""
    add(
        "recipes",
        ids=[f"{workbook_id}::recipe::{abs(hash((workbook_id, question)))}"],
        documents=[f"Question: {question}"],
        metadatas=[{"workbook_id": workbook_id, "sql": sql, "question": question}],
    )


def remember_insight(workbook_id: str, finding: str) -> None:
    add(
        "insights",
        ids=[f"{workbook_id}::insight::{abs(hash((workbook_id, finding)))}"],
        documents=[finding],
        metadatas=[{"workbook_id": workbook_id}],
    )

"""FastAPI entrypoint.

Endpoints (the contract the frontend in excel-analyst-ui speaks):
    GET  /api/health           liveness; ?deep=true also probes the model provider
    POST /api/workbooks        multipart upload -> Workbook
    GET  /api/workbooks/{id}   -> Workbook
    POST /api/chat             -> text/event-stream of pipeline events
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from . import config, llm, pipeline, store, vectors
from .profiling import describe_column, load_workbook

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("sheetsense")

app = FastAPI(title="Sheetsense API", version="0.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

ALLOWED_SUFFIXES = (".xlsx", ".xlsm", ".csv")


@app.on_event("startup")
def announce_config() -> None:
    log.info("provider=%s", config.LLM_PROVIDER)
    for key, value in config.describe().items():
        log.info("  %s=%s", key, value)
    if config.LLM_PROVIDER == "mistral" and not config.MISTRAL_API_KEY:
        log.error("LLM_PROVIDER=mistral but MISTRAL_API_KEY is empty — calls will fail.")


@app.get("/api/health")
def health(deep: bool = Query(False, description="Also probe the model provider")) -> dict[str, Any]:
    """Liveness. `deep=true` costs one embedding call, so keep it off uptime pings."""
    payload: dict[str, Any] = {"status": "ok", **config.describe()}
    if deep:
        payload["provider_health"] = llm.health()
    return payload


def _index_columns(workbook_id: str, sheets: list[dict[str, Any]]) -> int:
    """Embed one document per column. llm.embed() handles batching internally."""
    ids: list[str] = []
    documents: list[str] = []
    metadatas: list[dict[str, Any]] = []

    for sheet in sheets:
        for column in sheet["columns"]:
            ids.append(f"{workbook_id}::{sheet['name']}::{column['column']}")
            documents.append(describe_column(column))
            metadatas.append(
                {
                    "workbook_id": workbook_id,
                    "sheet": sheet["name"],
                    "column": column["column"],
                    "dtype": column["dtype"],
                    "role": column["role"],
                }
            )

    vectors.add("columns", ids, documents, metadatas)
    return len(ids)


def _default_questions(sheets: list[dict[str, Any]]) -> list[str]:
    first = sheets[0]
    measures = [c["column"] for c in first["columns"] if c["role"] == "measure"][:1]
    dimensions = [c["column"] for c in first["columns"] if c["role"] == "dimension"][:1]

    questions = [f"How many rows are in {first['name']}?"]
    if measures:
        questions.append(f"What is the total {measures[0]}?")
        if dimensions:
            questions.append(f"Show {measures[0]} broken down by {dimensions[0]}.")
    questions.append("Are there any columns with missing values?")
    return questions[:4]


def _summarise(filename: str, sheets: list[dict[str, Any]]) -> tuple[str, list[str]]:
    """Best-effort workbook summary; falls back to a factual description."""
    outline = "\n".join(
        f'{sheet["name"]}: {sheet["rows"]} rows — '
        + ", ".join(c["column"] for c in sheet["columns"][:12])
        for sheet in sheets
    )
    fallback = (
        f"{filename} contains {len(sheets)} sheet(s) and "
        f"{sum(sheet['rows'] for sheet in sheets):,} rows in total."
    )

    try:
        result = llm.complete_json(
            "You describe spreadsheets for an analyst who has not opened them yet.",
            f"""Workbook: {filename}

{outline}

Return keys:
  summary: two sentences describing what this data is and its grain.
  questions: an array of 4 specific questions worth asking of this data.
""",
        )
        summary = str(result.get("summary") or fallback)
        questions = [str(q) for q in (result.get("questions") or []) if str(q).strip()][:4]
        return summary, questions or _default_questions(sheets)
    except Exception as error:
        log.warning("summary generation failed, using fallback: %s", error)
        return fallback, _default_questions(sheets)


@app.post("/api/workbooks")
async def upload_workbook(file: UploadFile = File(...)) -> dict[str, Any]:
    name = (file.filename or "").lower()
    if not name.endswith(ALLOWED_SUFFIXES):
        raise HTTPException(400, "Only .xlsx, .xlsm and .csv files are supported.")

    payload = await file.read()
    if not payload:
        raise HTTPException(400, "That file appears to be empty.")
    if len(payload) > config.MAX_UPLOAD_BYTES:
        raise HTTPException(413, "That file exceeds the upload size limit.")

    workbook_id = uuid.uuid4().hex[:12]
    filename = file.filename or "workbook"
    log.info("upload %s (%s, %.1f KB)", workbook_id, filename, len(payload) / 1024)

    # Parsed straight from memory — the original file is never written to disk.
    try:
        sheets = load_workbook(payload, filename, store.connect(workbook_id))
    except Exception as error:
        log.warning("parse failed for %s: %s", filename, error)
        raise HTTPException(400, f"Could not read that workbook: {error}") from error

    vectors.forget_workbook(workbook_id)
    try:
        indexed = _index_columns(workbook_id, sheets)
        log.info("indexed %d columns for %s", indexed, workbook_id)
    except Exception as error:
        store.discard(workbook_id)
        detail = (
            "Could not build the index — is Ollama running and the embedding model pulled?"
            if config.LLM_PROVIDER == "ollama"
            else "Could not build the index — check MISTRAL_API_KEY and your rate limits."
        )
        raise HTTPException(503, f"{detail} ({error})") from error

    summary, questions = _summarise(filename, sheets)
    return store.save(workbook_id, filename, sheets, summary, questions)


@app.get("/api/workbooks/{workbook_id}")
def get_workbook(workbook_id: str) -> dict[str, Any]:
    record = store.get(workbook_id)
    if record is None:
        raise HTTPException(404, "Workbook not found.")
    return record


class GlossaryEntry(BaseModel):
    term: str
    definition: str
    sql: str = ""


@app.get("/api/workbooks/{workbook_id}/glossary")
def get_glossary(workbook_id: str) -> list[dict[str, Any]]:
    if store.get(workbook_id) is None:
        raise HTTPException(404, "Workbook not found.")
    return vectors.list_glossary(workbook_id)


@app.post("/api/workbooks/{workbook_id}/glossary")
def put_glossary(workbook_id: str, entry: GlossaryEntry) -> dict[str, Any]:
    """Teach the system one business term. Re-posting the same term replaces it."""
    if store.get(workbook_id) is None:
        raise HTTPException(404, "Workbook not found.")
    if not entry.term.strip() or not entry.definition.strip():
        raise HTTPException(400, "Both a term and a definition are required.")
    try:
        return vectors.add_glossary(workbook_id, entry.term, entry.definition, entry.sql)
    except Exception as error:
        raise HTTPException(503, f"Could not save the term: {error}") from error


@app.delete("/api/workbooks/{workbook_id}/glossary/{term}")
def remove_glossary(workbook_id: str, term: str) -> dict[str, str]:
    vectors.delete_glossary(workbook_id, term)
    return {"status": "deleted", "term": term}


class ChatRequest(BaseModel):
    workbook_id: str
    question: str


@app.post("/api/chat")
def chat(request: ChatRequest) -> StreamingResponse:
    if not request.question.strip():
        raise HTTPException(400, "The question is empty.")

    def event_stream():
        for event in pipeline.answer(request.workbook_id, request.question):
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # stop nginx buffering the stream
        },
    )

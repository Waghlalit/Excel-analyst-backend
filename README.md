# Sheetsense — backend

FastAPI service that turns a spreadsheet into an answerable database.

The model never sees your rows. It sees a **profile** of the schema, writes SQL
against it, and the query runs over the whole dataset. Retrieval finds *where*
to look; the database computes the answer.

> Full setup instructions — including account creation and the frontend — are in
> the [root README](../README.md). This file covers the backend specifics.

```
upload ──▶ pandas ──▶ DuckDB / MotherDuck
                 └──▶ column profiles ──▶ ChromaDB / Chroma Cloud

question ──▶ analyse ──▶ retrieve ──▶ write SQL ──▶ validate ──▶ execute
                                                            └──▶ summarise + chart
```

## Quick start

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env      # then fill in your keys
python -m uvicorn app.main:app --reload --port 8000
```

## Two ways to run it

Both are selected entirely by `.env` — no code changes.

| | Local | Hosted |
|---|---|---|
| Models | Ollama on localhost | Mistral API |
| Tabular data | `.duckdb` files under `DATA_DIR` | MotherDuck |
| Vectors | Chroma on disk | Chroma Cloud |
| Nothing leaves the machine | ✅ | ❌ |
| Deployable to a free tier | ❌ (can't host a 7B model free) | ✅ |

Local mode needs `ollama pull qwen2.5-coder:7b` and `ollama pull nomic-embed-text`.

> ⚠️ **Embedding dimensions differ per provider** (nomic 768, mistral 1024).
> Collections are namespaced by embedding model, so switching starts a clean
> index instead of mixing incompatible vectors. Re-upload after a switch.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/health` | Liveness + active config. `?deep=true` probes the provider |
| `POST` | `/api/workbooks` | Multipart upload → parse, profile, index, return the workbook |
| `GET` | `/api/workbooks/{id}` | Fetch a stored workbook |
| `GET` | `/api/workbooks/{id}/glossary` | List business terms |
| `POST` | `/api/workbooks/{id}/glossary` | Add or replace a term |
| `DELETE` | `/api/workbooks/{id}/glossary/{term}` | Remove a term |
| `POST` | `/api/chat` | `{workbook_id, question}` → SSE stream |

### Chat stream events

Newline-delimited SSE frames, one JSON object each:

| `type` | Payload |
|---|---|
| `status` | `stage`: `analyzing` \| `retrieving` \| `computing` \| `writing` |
| `sources` | Retrieved columns / glossary / recipes / insights |
| `delta` | A chunk of the narrative |
| `block` | A `code`, `table` or `chart` block |
| `done` / `error` | Terminal |

## The three model roles

They have genuinely different requirements, so they are configured separately.
On a paid API, routing all three to one model wastes money.

| Role | Job | Default (Mistral) |
|---|---|---|
| `CLASSIFIER_MODEL` | Intent, chart-needed, time grain, measure | `mistral-small-latest` |
| `SQL_MODEL` | Writes the query — decides correctness | `codestral-latest` |
| `NARRATIVE_MODEL` | Prose over already-computed numbers | `mistral-small-latest` |

`LLM_MODEL=x` overrides all three at once — useful locally when only one model
is pulled.

## Vector collections

| Collection | Contents | Why it exists |
|---|---|---|
| `columns` | One doc per column: name, type, range, samples | Hundreds of columns won't fit in a prompt |
| `glossary` | Business term → SQL fragment | "Revenue" must mean the same thing every time |
| `recipes` | Past question → working SQL | Retrieved as few-shot; improves with use |
| `insights` | Findings from earlier turns | Follow-ups build on established context |

Every query filters on `workbook_id`, so uploads are isolated from each other.

**The glossary is the highest-leverage feature.** One definition can add a filter
the model would otherwise miss on every future question.

## Safety

- `sqlguard.validate()` parses generated SQL with sqlglot: single statement,
  SELECT only, known tables and columns, no DDL/DML, and a forced `LIMIT`
- Queries execute against a **read-only** connection — a `DELETE` fails at the
  database, not in Python
- One repair attempt when validation fails; most failures are a hallucinated
  column name and survive a single retry
- Uploaded files are parsed **in memory** and never written to disk

## Known limits

- Merged cells and multiple tables on one sheet confuse the column profiler
- Formulas are read as their computed values, not re-evaluated
- A local 7B model writes weaker SQL than a hosted coding model — a deliberate
  trade for privacy and cost

## Layout

| File | Role |
|---|---|
| `app/main.py` | FastAPI app and routes |
| `app/pipeline.py` | analyse → retrieve → SQL → execute → summarise |
| `app/profiling.py` | Excel/CSV → DuckDB + column profiles |
| `app/vectors.py` | The four Chroma collections |
| `app/sqlguard.py` | SQL validation |
| `app/store.py` | Storage backend + workbook registry |
| `app/llm.py` | Provider abstraction, retry, batching |
| `app/config.py` | Environment configuration |

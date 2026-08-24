<div align="center">

# 📊 Sheetsense — Backend

**Ask a spreadsheet anything. Get an answer computed over every single row — with the SQL that produced it attached.**

[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![DuckDB](https://img.shields.io/badge/DuckDB-FFF000?logo=duckdb&logoColor=black)](https://duckdb.org)
[![Chroma](https://img.shields.io/badge/ChromaDB-FF6B6B)](https://trychroma.com)
[![Docker](https://img.shields.io/badge/Docker-2496ED?logo=docker&logoColor=white)](https://docker.com)

🎨 **[Frontend repository →](https://github.com/Waghlalit/Excel-analyst-front)**

</div>

---

## 🤔 The problem

You have a spreadsheet with 40,000 rows. You want last quarter's total.

The standard "chat with your data" approach embeds your rows into a vector
database, retrieves the few most similar to your question, and lets the model
answer from those.

**That cannot add up.**

Vector search returns what is *similar*. A total needs what is *complete*. Ask
for Q3 revenue and you get the eight rows that most resemble your question,
summed — a number that is fluent, precise, and wrong. No amount of extra
retrieval fixes it, because summing was never a retrieval problem.

---

## 💡 The approach

> **Retrieval finds *where* to look. The database computes the answer.**

The model never sees your rows. It sees a compact **profile** of your schema —
column names, types, ranges, sample values — writes SQL against that, and the
query runs over the entire dataset inside DuckDB. Only the small result set
comes back.

| | Naive RAG | Sheetsense |
|---|---|---|
| What gets embedded | Your data rows | Column meanings, business terms, past SQL |
| Who does the maths | The language model | 🦆 DuckDB |
| Rows considered | Top-k (a handful) | **All of them** |
| Can you verify the answer? | No | Yes — the SQL is returned |

---

## 🏗️ Architecture

**When a workbook is uploaded:**

```
                        ┌──────────────────────────────┐
   .xlsx / .csv ───────▶│   pandas — parse & profile   │
                        └───────────────┬──────────────┘
                                        │
                 ┌──────────────────────┴──────────────────────┐
                 ▼                                             ▼
     ┌────────────────────────┐                  ┌────────────────────────┐
     │  DuckDB / MotherDuck   │                  │ Chroma / Chroma Cloud  │
     ├────────────────────────┤                  ├────────────────────────┤
     │  every row lives here  │                  │  column profiles,      │
     │  → the numbers         │                  │  glossary, recipes     │
     │                        │                  │  → finding columns     │
     └────────────────────────┘                  └────────────────────────┘
```

**When a question is asked:**

```
  question
     │
     ▼
  analyse ──▶ retrieve ──▶ write SQL ──▶ validate ──▶ execute ──▶ summarise
  intent,     4 vector      codestral     sqlglot      read-only    narrative
  chart?,     collections                 AST check    DuckDB       + chart
  grain
```

---

## 🔄 How a question is answered

| # | Stage | What happens |
|---|---|---|
| 1 | **Analyse** | Intent, whether a chart fits, time grain, what is being measured |
| 2 | **Retrieve** | Four Chroma collections, every query filtered by `workbook_id` |
| 3 | **Write SQL** | Schema + retrieved columns + glossary terms + past working queries |
| 4 | **Validate** | sqlglot AST — one statement, `SELECT` only, known tables, forced `LIMIT` |
| 5 | **Execute** | Read-only connection, across the **full** dataset |
| 6 | **Summarise** | Narrative streamed token by token; chart type chosen from the data shape |
| 7 | **Remember** | Working SQL is stored as a retrievable example for next time |

Step 7 is why the system gets better at *your* spreadsheet the more you use it.

---

## 🧠 The four vector collections

None of them contain a single row of your data.

| Collection | Contents | Why it exists |
|---|---|---|
| `columns` | One document per column — name, type, range, samples | Hundreds of columns will not fit in a prompt |
| `glossary` | Business term → SQL fragment | "Revenue" must mean the same thing every time |
| `recipes` | Past question → the SQL that worked | Retrieved as few-shot examples; improves with use |
| `insights` | Findings from earlier turns | Follow-up questions build on established context |

> ### ⭐ The glossary is the highest-leverage feature
>
> A user defines **"active customer"** once in the chat sidebar. Every future
> question inherits that definition — including filters the model would
> otherwise miss. One sentence of business vocabulary permanently improves
> every answer after it. No fine-tuning. No re-indexing.

---

## 🚀 Quick start

### Prerequisites

- **Python 3.11+**

Nothing else runs locally in the default hosted configuration.

### 1️⃣ Get your keys

Three services, all with a free tier:

| Service | Used for | Where |
|---|---|---|
| 🤖 **Mistral** | SQL generation, classification, narrative, embeddings | [admin.mistral.ai](https://admin.mistral.ai) → API Keys |
| 🔍 **Chroma Cloud** | Hosted vector store | [trychroma.com](https://trychroma.com) → your database → Connect |
| 🦆 **MotherDuck** | Hosted DuckDB — where the spreadsheet data lives | [app.motherduck.com](https://app.motherduck.com) → Settings → Access Tokens |

> 💡 Prefer no accounts at all? See [Running fully offline](#-running-fully-offline).

### 2️⃣ Create `.env`

```ini
LLM_PROVIDER=mistral
MISTRAL_API_KEY=

CHROMA_API_KEY=
CHROMA_TENANT=
CHROMA_DATABASE=sheetsense

MOTHERDUCK_TOKEN=

EMBED_BATCH_SIZE=32
API_MAX_RETRIES=4
API_BACKOFF_SECONDS=1.5
MAX_RESULT_ROWS=200
DATA_DIR=./data
```

> 🔒 This file is gitignored. **Never commit it.**

### 3️⃣ Install and run

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m uvicorn app.main:app --reload --port 8000
```

> ⚠️ `pip` prints a dependency-conflict warning about `opentelemetry` —
> `mistralai` and `chromadb` disagree on a pin. It is expected and harmless.
> Install from the file rather than package by package; the order matters.

### 4️⃣ Verify

```powershell
curl "http://127.0.0.1:8000/api/health?deep=true"
```

```json
{
  "provider": "mistral",
  "sql_model": "codestral-latest",
  "embed_model": "mistral-embed",
  "provider_health": { "reachable": true, "embed_dimensions": 1024 }
}
```

`embed_dimensions: 1024` confirms Mistral is answering. Local Ollama returns 768.

---

## 🐳 Docker

```bash
docker build -t sheetsense-api:v1 .

docker run -d --name api \
  --restart unless-stopped \
  -p 8000:8000 \
  --env-file .env \
  -e DATA_DIR=/data \
  sheetsense-api:v1
```

Two things worth knowing:

- **`-e DATA_DIR=/data` matters.** A relative path like `./data` resolves to
  `/app/data` inside the container, which is probably not what you meant.
- **With MotherDuck and Chroma Cloud configured, this service is stateless.**
  Nothing touches the local filesystem, so containers can be destroyed and
  recreated freely, and no volume is required.

The image runs as a non-root user and ships a `HEALTHCHECK` against
`/api/health`.

---

## 🔌 API

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
| `done` / `error` | Terminal frame |

---

## 🤖 The three model roles

They have genuinely different requirements, so they are configured separately.
On a paid API, routing all three to one model wastes money.

| Role | Job | Default (Mistral) |
|---|---|---|
| `CLASSIFIER_MODEL` | Intent, chart-needed, time grain, measure | `mistral-small-latest` |
| `SQL_MODEL` | Writes the query — **decides correctness** | `codestral-latest` |
| `NARRATIVE_MODEL` | Prose over already-computed numbers | `mistral-small-latest` |

`LLM_MODEL=x` overrides all three at once — handy locally when only one model is
pulled.

---

## 🔀 Two ways to run it

Selected entirely by `.env`. No code changes.

| | 🏠 Local | ☁️ Hosted |
|---|---|---|
| Models | Ollama on localhost | Mistral API |
| Tabular data | `.duckdb` files under `DATA_DIR` | MotherDuck |
| Vectors | Chroma on disk | Chroma Cloud |
| Nothing leaves the machine | ✅ | ❌ |
| Deployable on a free tier | ❌ (a 7B model cannot be hosted free) | ✅ |

### 🏠 Running fully offline

Everything works without an internet connection using [Ollama](https://ollama.com):

```powershell
ollama pull qwen2.5-coder:7b     # SQL
ollama pull nomic-embed-text     # embeddings
```

```ini
LLM_PROVIDER=ollama
MOTHERDUCK_TOKEN=      # empty → local .duckdb files
CHROMA_API_KEY=        # empty → local Chroma
```

> ⚠️ **Embedding dimensions differ between providers** (nomic 768, mistral 1024).
> Collections are namespaced by embedding model, so switching starts a clean
> index instead of mixing incompatible vectors. **Re-upload after a switch.**

---

## 🛡️ Safety

- `sqlguard.validate()` parses generated SQL with **sqlglot**: single statement,
  `SELECT` only, known tables and columns, no DDL/DML, and a forced `LIMIT`
- Queries execute against a **read-only** connection — a `DELETE` fails at the
  database, not in Python. That is the real defence; validation just fails
  earlier and with a better message
- **One repair attempt** when validation fails. Most failures are a hallucinated
  column name and survive a single retry
- Uploaded files are parsed **in memory** and never written to disk
- Every retrieval is filtered by `workbook_id`, so uploads are isolated

---

## ⚙️ Configuration reference

<details>
<summary><b>All environment variables</b></summary>

<br>

| Variable | Default | Notes |
|---|---|---|
| `LLM_PROVIDER` | `ollama` | `ollama` or `mistral` |
| `MISTRAL_API_KEY` | — | Required when provider is `mistral` |
| `OLLAMA_HOST` | `http://127.0.0.1:11434` | Use `http://host.docker.internal:11434` inside Docker |
| `CLASSIFIER_MODEL` | per provider | Small model — question classification |
| `SQL_MODEL` | per provider | The strongest model; decides answer correctness |
| `NARRATIVE_MODEL` | per provider | Prose over already-computed numbers |
| `EMBED_MODEL` | per provider | Changing this invalidates the index |
| `LLM_MODEL` | — | Overrides all three chat roles at once |
| `MOTHERDUCK_TOKEN` | — | Empty = local `.duckdb` files |
| `CHROMA_API_KEY` / `CHROMA_TENANT` | — | Both required for Chroma Cloud |
| `EMBED_BATCH_SIZE` | `32` | Lower it if you hit rate limits |
| `API_MAX_RETRIES` | `4` | Retries on transient provider errors |
| `MAX_RESULT_ROWS` | `200` | Cap on rows **returned**, not rows scanned |
| `MAX_UPLOAD_BYTES` | `52428800` | 50 MB |
| `DATA_DIR` | `./data` | Only used when both hosted blocks are empty |
| `CORS_ORIGINS` | localhost:5173 | Set to your real domain in production |

</details>

---

## ⚠️ Known limits

- Merged cells and multiple tables on one sheet confuse the column profiler
- Formulas are read as their computed values, not re-evaluated
- A local 7B model writes weaker SQL than a hosted coding model — a deliberate
  trade for privacy and cost

---

## 🔧 Troubleshooting

<details>
<summary><b>Common problems and their causes</b></summary>

<br>

| Symptom | Cause |
|---|---|
| `Your DuckDB version is not supported by MotherDuck` | Needs `duckdb>=1.5.5` — reinstall from `requirements.txt` |
| Upload fails with `503` | Provider unreachable — check the key, or that Ollama is running |
| Answers ignore a filter you expect | Add a glossary term for it |
| `WinError 10013` on port 8000 | Something is already listening — kill it or change the port |
| Empty chat after switching provider | Embedding dimensions changed — re-upload your workbooks |
| Docker build killed | Out of memory — the build needs roughly 1 GB of free RAM |

</details>

---

## 🧩 Project layout

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

---

<div align="center">

**Built with** FastAPI · DuckDB · Chroma · Mistral · sqlglot · pandas

🎨 **[Frontend repository →](https://github.com/Waghlalit/Excel-analyst-front)**

</div>

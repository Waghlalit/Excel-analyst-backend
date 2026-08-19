"""The answer pipeline: analyse -> retrieve -> compute -> summarise.

Emits the SSE event shapes the frontend consumes (see src/lib/api.ts):
    status | sources | delta | block | done | error
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

from . import llm, store, vectors
from .sqlguard import UnsafeQuery, validate

ANALYST_SYSTEM = (
    "You are a careful data analyst working over a DuckDB database that was loaded "
    "from a spreadsheet. You never invent column names."
)


def _analyse(question: str) -> dict[str, Any]:
    # Uses the classifier role (a small, cheap model) — this is just labelling.
    result = llm.complete_json(
        ANALYST_SYSTEM,
        f"""Classify this question about a spreadsheet.

Question: {question}

Return exactly these keys:
  intent: one of "aggregate", "trend", "comparison", "outlier", "lookup", "text_search"
  needs_chart: true or false
  time_grain: one of "none", "day", "month", "quarter", "year"
  measure: two or three words naming the value being measured
           (e.g. "cancellation percentage", "total revenue"), or "" if none
""",
    )
    return {
        "intent": result.get("intent", "lookup"),
        "needs_chart": bool(result.get("needs_chart", False)),
        "time_grain": result.get("time_grain", "none"),
        "measure": str(result.get("measure") or "").strip().lower(),
    }


def _retrieve(workbook_id: str, question: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Gather retrieval context and the source list shown in the UI."""
    columns = vectors.query("columns", question, workbook_id, k=15)
    glossary = vectors.query("glossary", question, workbook_id, k=3)
    recipes = vectors.query("recipes", question, workbook_id, k=3)
    insights = vectors.query("insights", question, workbook_id, k=2)

    sources: list[dict[str, Any]] = []
    for document, metadata, score in columns[:8]:
        sources.append(
            {
                "kind": "column",
                "label": f"{metadata.get('sheet')}.{metadata.get('column')}",
                "detail": document.split("\n")[2] if "\n" in document else document[:80],
                "score": round(score, 3),
            }
        )
    for document, metadata, score in glossary:
        sources.append(
            {
                "kind": "glossary",
                "label": str(metadata.get("term", "term")),
                "detail": document[:120],
                "score": round(score, 3),
            }
        )
    for _, metadata, score in recipes:
        sources.append(
            {
                "kind": "recipe",
                "label": str(metadata.get("question", ""))[:60],
                "detail": "Previously answered — reused as an example",
                "score": round(score, 3),
            }
        )
    for document, _, score in insights:
        sources.append(
            {"kind": "insight", "label": "Earlier finding", "detail": document[:120],
             "score": round(score, 3)}
        )

    context = {
        "columns": [document for document, _, _ in columns],
        "glossary": [f"{document}\nSQL: {metadata.get('sql', '')}" for document, metadata, _ in glossary],
        "recipes": [
            f"Q: {metadata.get('question')}\nSQL: {metadata.get('sql')}"
            for _, metadata, _ in recipes
            if metadata.get("sql")
        ],
        "insights": [document for document, _, _ in insights],
    }
    return sources, context


def _write_sql(question: str, schema: str, context: dict[str, Any]) -> str:
    parts = [f"Database schema:\n{schema}"]
    if context["columns"]:
        parts.append("Relevant columns:\n" + "\n\n".join(context["columns"][:10]))
    if context["glossary"]:
        parts.append("Business definitions (use these exactly):\n" + "\n".join(context["glossary"]))
    if context["recipes"]:
        parts.append("Similar questions answered before:\n" + "\n\n".join(context["recipes"]))
    parts.append(f"Question: {question}")
    parts.append(
        "Write ONE DuckDB SELECT statement that answers the question. "
        "Use only the tables and columns above. Alias aggregates with clear names. "
        "Return only SQL."
    )

    # The SQL role gets the strongest available model — this is the step that
    # decides whether the answer is right.
    raw = llm.complete(ANALYST_SYSTEM, "\n\n".join(parts), role="sql", temperature=0.0)
    return llm.strip_sql_fence(raw)


_PERCENT_HINTS = ("pct", "percent", "percentage", "rate", "ratio", "share")
_CURRENCY_HINTS = ("revenue", "total", "amount", "sales", "cost", "price", "value", "spend")


def _infer_unit(column: str) -> str | None:
    lowered = column.lower()
    if any(hint in lowered for hint in _PERCENT_HINTS):
        return "percent"
    if any(hint in lowered for hint in _CURRENCY_HINTS):
        return "currency"
    return None


def _pick_value_column(columns: list[str], row: list[Any], measure: str) -> int | None:
    """Choose which numeric column the chart should plot.

    A result often has several numeric columns — "cancellation % by region"
    returns total, cancelled and pct. Plotting the first one silently answers a
    different question than the user asked, so the choice is made in three
    steps, most reliable first.
    """
    numeric = [i for i, value in enumerate(row) if isinstance(value, (int, float)) and i != 0]
    if not numeric:
        return None

    # 1. The classifier told us what is being measured — match it against the
    #    column names by shared words.
    if measure:
        words = {word for word in re.split(r"\W+", measure) if len(word) > 3}
        if words:
            best, best_score = None, 0
            for index in numeric:
                name_words = set(re.split(r"[\W_]+", columns[index].lower()))
                score = len(words & name_words)
                if score > best_score:
                    best, best_score = index, score
            if best is not None:
                return best

    # 2. SQL convention puts the computed measure last, so prefer the rightmost
    #    numeric column over the leftmost.
    return numeric[-1]


def _build_chart(
    question: str, columns: list[str], rows: list[list[Any]], analysis: dict[str, Any]
) -> dict[str, Any] | None:
    """Turn a small categorical/time result into a chart block, if the shape fits."""
    if not analysis["needs_chart"] or len(columns) < 2 or not rows:
        return None
    if len(rows) > 24:
        return None

    label_index = 0
    value_index = _pick_value_column(columns, rows[0], analysis.get("measure", ""))
    if value_index is None:
        return None

    points = [
        {"label": str(row[label_index]), "value": float(row[value_index])}
        for row in rows
        if isinstance(row[value_index], (int, float))
    ]
    if not points:
        return None

    # A time grain means the x axis is ordered — a line reads that better.
    chart = "line" if analysis["time_grain"] != "none" else "bar"

    block: dict[str, Any] = {
        "type": "chart",
        "chart": chart,
        "title": question[:80],
        "x_label": columns[label_index],
        "y_label": columns[value_index],
        "series": [{"name": columns[value_index], "points": points}],
    }
    unit = _infer_unit(columns[value_index])
    if unit:
        block["unit"] = unit
    return block


def answer(workbook_id: str, question: str) -> Iterator[dict[str, Any]]:
    record = store.get(workbook_id)
    if record is None:
        yield {"type": "error", "message": "That workbook could not be found."}
        yield {"type": "done"}
        return

    sheets = record["sheets"]
    allowed_tables = {sheet["name"] for sheet in sheets}
    allowed_columns = {
        column["column"] for sheet in sheets for column in sheet["columns"]
    }
    schema = "\n".join(
        f'TABLE "{sheet["name"]}" ({sheet["rows"]} rows): '
        + ", ".join(f"{c['column']} {c['dtype']}" for c in sheet["columns"])
        for sheet in sheets
    )

    try:
        yield {"type": "status", "stage": "analyzing"}
        analysis = _analyse(question)

        yield {"type": "status", "stage": "retrieving"}
        sources, context = _retrieve(workbook_id, question)
        yield {"type": "sources", "sources": sources}

        yield {"type": "status", "stage": "computing"}
        sql = _write_sql(question, schema, context)

        try:
            safe_sql = validate(sql, allowed_tables, allowed_columns)
        except UnsafeQuery as error:
            # One repair attempt with the error fed back — most failures are a
            # hallucinated column name and are fixable in a single retry.
            repaired = llm.complete(
                ANALYST_SYSTEM,
                f"This DuckDB query was rejected: {error}\n\n{sql}\n\n"
                f"Schema:\n{schema}\n\nReturn a corrected SELECT. SQL only.",
                role="sql",
                temperature=0.0,
            )
            safe_sql = validate(llm.strip_sql_fence(repaired), allowed_tables, allowed_columns)

        yield {"type": "block", "block": {"type": "code", "language": "sql", "code": safe_sql}}

        columns, rows, total = store.run_query(workbook_id, safe_sql)

        table_block: dict[str, Any] = {"type": "table", "columns": columns, "rows": rows}
        if total > len(rows):
            table_block["truncated_from"] = total
        yield {"type": "block", "block": table_block}

        chart = _build_chart(question, columns, rows, analysis)
        if chart:
            yield {"type": "block", "block": chart}

        yield {"type": "status", "stage": "writing"}
        preview = [columns, *rows[:15]]
        narrative_parts: list[str] = []
        for piece in llm.stream(
            ANALYST_SYSTEM,
            f"""Question: {question}

Query that ran:
{safe_sql}

Result ({total} rows, showing up to 15):
{preview}

Write a short answer in plain language. Lead with the number or finding that
answers the question. Two or three sentences. Do not repeat the SQL. Do not
invent figures that are not in the result.""",
        ):
            narrative_parts.append(piece)
            yield {"type": "delta", "text": piece}

        # Successful turns become retrievable context for later questions.
        vectors.remember_recipe(workbook_id, question, safe_sql)
        narrative = "".join(narrative_parts).strip()
        if narrative:
            vectors.remember_insight(workbook_id, f"Q: {question}\nA: {narrative}")

    except UnsafeQuery as error:
        yield {"type": "error", "message": str(error)}
    except Exception as error:  # surfaced to the user rather than a blank turn
        yield {"type": "error", "message": f"{type(error).__name__}: {error}"}

    yield {"type": "done"}

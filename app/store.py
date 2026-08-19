"""Workbook storage.

Two backends, chosen by whether MOTHERDUCK_TOKEN is set:

    local        one .duckdb file per workbook, under DATA_DIR
    motherduck   one hosted database per workbook

Both use the same SQL dialect, so the prompts and sqlguard are identical either
way — the only difference is where the bytes live.

The workbook *registry* lives in the database too, not in a JSON file. On an
ephemeral container filesystem a local JSON index is lost on every restart,
which would leave the data intact but the app unable to find it.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb

from . import config
from .config import DB_DIR, MAX_RESULT_ROWS

REGISTRY_DB = "sheetsense_registry"


def db_path(workbook_id: str) -> Path:
    return DB_DIR / f"{workbook_id}.duckdb"


def _database_name(workbook_id: str) -> str:
    """MotherDuck database name for a workbook. One database per workbook keeps
    table names unqualified, so the SQL the model writes is identical in both
    modes and no prompt changes are needed."""
    return f"wb_{workbook_id}"


def connect(workbook_id: str, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    """Open a workbook's database.

    `read_only` is the real safety boundary for query execution: validated SQL
    still runs against a connection that physically cannot write.
    """
    if config.USE_MOTHERDUCK:
        name = _database_name(workbook_id)
        connection = duckdb.connect(f"md:?motherduck_token={config.MOTHERDUCK_TOKEN}")
        if not read_only:
            connection.execute(f'CREATE DATABASE IF NOT EXISTS "{name}"')
        connection.execute(f'USE "{name}"')
        return connection

    return duckdb.connect(str(db_path(workbook_id)), read_only=read_only)


# --------------------------------------------------------------------------- registry


def _registry() -> duckdb.DuckDBPyConnection:
    """Connection to the registry database, with the table ensured."""
    if config.USE_MOTHERDUCK:
        connection = duckdb.connect(f"md:?motherduck_token={config.MOTHERDUCK_TOKEN}")
        connection.execute(f'CREATE DATABASE IF NOT EXISTS "{REGISTRY_DB}"')
        connection.execute(f'USE "{REGISTRY_DB}"')
    else:
        connection = duckdb.connect(str(DB_DIR / "_registry.duckdb"))

    # The record is stored as JSON in one column rather than exploded into
    # typed columns — the shape is nested and read whole, so a schema here
    # would buy nothing and cost a migration on every change.
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS workbooks (
            id          VARCHAR PRIMARY KEY,
            filename    VARCHAR,
            uploaded_at TIMESTAMP,
            record      VARCHAR
        )
        """
    )
    return connection


def save(
    workbook_id: str,
    filename: str,
    sheets: list[dict[str, Any]],
    summary: str,
    suggested: list[str],
) -> dict[str, Any]:
    record = {
        "id": workbook_id,
        "filename": filename,
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
        "sheets": sheets,
        "summary": summary,
        "suggested_questions": suggested,
    }

    connection = _registry()
    try:
        connection.execute("DELETE FROM workbooks WHERE id = ?", [workbook_id])
        connection.execute(
            "INSERT INTO workbooks (id, filename, uploaded_at, record) VALUES (?, ?, ?, ?)",
            [workbook_id, filename, record["uploaded_at"], json.dumps(record, default=str)],
        )
        connection.commit()
    finally:
        connection.close()

    return record


def get(workbook_id: str) -> dict[str, Any] | None:
    connection = _registry()
    try:
        row = connection.execute(
            "SELECT record FROM workbooks WHERE id = ?", [workbook_id]
        ).fetchone()
    finally:
        connection.close()

    if not row:
        return None
    try:
        return json.loads(row[0])
    except json.JSONDecodeError:
        return None


def list_workbooks(limit: int = 50) -> list[dict[str, Any]]:
    """Recent workbooks, newest first — id/filename/date only, not the profiles."""
    connection = _registry()
    try:
        rows = connection.execute(
            "SELECT id, filename, uploaded_at FROM workbooks "
            "ORDER BY uploaded_at DESC LIMIT ?",
            [limit],
        ).fetchall()
    finally:
        connection.close()

    return [
        {
            "id": row[0],
            "filename": row[1],
            "uploaded_at": row[2].isoformat() if hasattr(row[2], "isoformat") else str(row[2]),
        }
        for row in rows
    ]


def discard(workbook_id: str) -> None:
    """Remove a workbook and its data.

    Used when indexing fails after the tables were written, so a failed upload
    doesn't leave an orphan database behind.
    """
    connection = _registry()
    try:
        connection.execute("DELETE FROM workbooks WHERE id = ?", [workbook_id])
        connection.commit()
    finally:
        connection.close()

    if config.USE_MOTHERDUCK:
        admin = duckdb.connect(f"md:?motherduck_token={config.MOTHERDUCK_TOKEN}")
        try:
            admin.execute(f'DROP DATABASE IF EXISTS "{_database_name(workbook_id)}"')
        finally:
            admin.close()
    else:
        db_path(workbook_id).unlink(missing_ok=True)


# --------------------------------------------------------------------------- queries


def run_query(workbook_id: str, sql: str) -> tuple[list[str], list[list[Any]], int]:
    """Execute validated SQL read-only. Returns (columns, rows, total_before_cap)."""
    if not config.USE_MOTHERDUCK and not db_path(workbook_id).exists():
        raise FileNotFoundError("This workbook is no longer available.")

    connection = connect(workbook_id, read_only=True)
    try:
        cursor = connection.execute(sql)
        columns = [description[0] for description in cursor.description]
        rows = cursor.fetchall()
    finally:
        connection.close()

    total = len(rows)
    trimmed = [
        [value.isoformat() if hasattr(value, "isoformat") else value for value in row]
        for row in rows[:MAX_RESULT_ROWS]
    ]
    return columns, trimmed, total

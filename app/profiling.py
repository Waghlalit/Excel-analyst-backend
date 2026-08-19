"""Load a workbook into DuckDB and build a compact profile of every column.

The profile — not the data — is what the model sees. A 12,000-row sheet becomes
roughly 40 tokens per column, which is why a 60-sheet workbook still fits in a
prompt while the arithmetic stays exact in the database.
"""

from __future__ import annotations

import io
import re
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

SAFE_NAME = re.compile(r"[^A-Za-z0-9_]")


def sanitize(name: str) -> str:
    """Turn an arbitrary sheet/column label into a safe SQL identifier."""
    cleaned = SAFE_NAME.sub("_", name.strip()) or "col"
    if cleaned[0].isdigit():
        cleaned = f"_{cleaned}"
    return cleaned[:60]


def _infer_role(series: pd.Series, name: str) -> str:
    lowered = name.lower()
    if pd.api.types.is_datetime64_any_dtype(series):
        return "date"
    if pd.api.types.is_numeric_dtype(series):
        # An all-unique integer column named *_id is a key, not a measure.
        if lowered.endswith(("_id", "_no", "_code")) and series.is_unique:
            return "key"
        return "measure"
    if series.dtype == object:
        non_null = series.dropna()
        if non_null.empty:
            return "text"
        distinct_ratio = non_null.nunique() / len(non_null)
        avg_length = non_null.astype(str).str.len().mean()
        # Long, mostly-unique strings are prose; short repeated ones are labels.
        if avg_length > 60 and distinct_ratio > 0.5:
            return "text"
        if distinct_ratio < 0.5:
            return "dimension"
        return "key" if series.is_unique else "dimension"
    return "text"


def _jsonable(value: Any) -> Any:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, (pd.Timestamp,)):
        return value.isoformat()
    if hasattr(value, "item"):  # numpy scalar
        try:
            return value.item()
        except Exception:  # pragma: no cover - defensive
            return str(value)
    return value


def profile_column(series: pd.Series, sheet: str, name: str) -> dict[str, Any]:
    non_null = series.dropna()
    role = _infer_role(series, name)

    profile: dict[str, Any] = {
        "sheet": sheet,
        "column": name,
        "dtype": str(series.dtype),
        "role": role,
        "null_pct": round(float(series.isna().mean() * 100), 2),
        "distinct": int(non_null.nunique()) if not non_null.empty else 0,
        "min": None,
        "max": None,
        "samples": [_jsonable(v) for v in non_null.head(4).tolist()],
    }

    if role in {"measure", "date"} and not non_null.empty:
        profile["min"] = _jsonable(non_null.min())
        profile["max"] = _jsonable(non_null.max())

    return profile


def describe_column(profile: dict[str, Any]) -> str:
    """The natural-language text that gets embedded for semantic retrieval.

    Prose here, exact identifiers in the metadata — questions are matched
    against the prose, but SQL is always built from the metadata.
    """
    lines = [
        f"Column: {profile['column']}",
        f"Sheet: {profile['sheet']}",
        f"Type: {profile['dtype']} ({profile['role']})",
    ]
    if profile["min"] is not None or profile["max"] is not None:
        lines.append(f"Range: {profile['min']} to {profile['max']}")
    if profile["distinct"]:
        lines.append(f"Distinct values: {profile['distinct']}")
    lines.append(f"Missing: {profile['null_pct']}%")
    if profile["samples"]:
        preview = ", ".join(str(sample) for sample in profile["samples"])
        lines.append(f"Sample values: {preview}")
    return "\n".join(lines)


def load_workbook(payload: bytes, filename: str, connection: Any) -> list[dict[str, Any]]:
    """Read every sheet into DuckDB and return one profile dict per sheet.

    Takes the upload as bytes rather than a path: once the rows are in DuckDB
    and the profiles are in the vector store, the original file is never read
    again, so there is no reason to persist it. That also means no object
    storage is needed when this runs on an ephemeral container filesystem.
    """
    name = filename.lower()
    buffer = io.BytesIO(payload)

    if name.endswith(".csv"):
        stem = Path(filename).stem
        frames = {stem: pd.read_csv(buffer)}
    else:
        # sheet_name=None reads every sheet into a dict.
        frames = pd.read_excel(buffer, sheet_name=None)

    if not frames:
        raise ValueError("The workbook contains no readable sheets.")

    sheets: list[dict[str, Any]] = []

    try:
        for raw_sheet_name, frame in frames.items():
            if frame.empty:
                continue

            frame = frame.rename(columns={c: sanitize(str(c)) for c in frame.columns})
            # Duplicate headers would collide as SQL identifiers.
            frame = frame.loc[:, ~frame.columns.duplicated()]

            # Try to recover dates that pandas left as strings.
            for column in frame.columns:
                if frame[column].dtype == object:
                    lowered = column.lower()
                    if any(token in lowered for token in ("date", "_dt", "time")):
                        converted = pd.to_datetime(frame[column], errors="coerce")
                        if converted.notna().mean() > 0.8:
                            frame[column] = converted

            table = sanitize(str(raw_sheet_name))
            connection.register("staging_frame", frame)
            connection.execute(f'CREATE OR REPLACE TABLE "{table}" AS SELECT * FROM staging_frame')
            connection.unregister("staging_frame")

            sheets.append(
                {
                    "name": table,
                    "rows": int(len(frame)),
                    "columns": [
                        profile_column(frame[column], table, column) for column in frame.columns
                    ],
                }
            )

        connection.commit()
    finally:
        # The caller owns the connection (it may be a MotherDuck session).
        connection.close()

    if not sheets:
        raise ValueError("Every sheet in the workbook was empty.")

    return sheets


def schema_prompt(sheets: list[dict[str, Any]]) -> str:
    """The compact DDL summary handed to the model when it writes SQL."""
    blocks = []
    for sheet in sheets:
        columns = ", ".join(
            f"{column['column']} {column['dtype']}" for column in sheet["columns"]
        )
        blocks.append(f'TABLE "{sheet["name"]}" ({sheet["rows"]} rows): {columns}')
    return "\n".join(blocks)

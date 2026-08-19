"""Validate model-written SQL before it touches the database.

The database user is read-only, which is the real defence. This layer catches
mistakes earlier and with a better error message, and enforces the row cap.
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp

from .config import MAX_RESULT_ROWS


class UnsafeQuery(Exception):
    """Raised when generated SQL fails validation."""


_FORBIDDEN = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Drop,
    exp.Create,
    exp.Alter,
    exp.TruncateTable,
)


def validate(sql: str, allowed_tables: set[str], allowed_columns: set[str]) -> str:
    """Return a safe, row-capped SELECT, or raise UnsafeQuery."""
    sql = sql.strip().rstrip(";").strip()
    if not sql:
        raise UnsafeQuery("The model returned an empty query.")

    try:
        statements = sqlglot.parse(sql, read="duckdb")
    except Exception as error:  # sqlglot raises several parse error types
        raise UnsafeQuery(f"Could not parse the generated SQL: {error}") from error

    statements = [statement for statement in statements if statement is not None]
    if len(statements) != 1:
        raise UnsafeQuery("Only a single statement is allowed.")

    statement = statements[0]

    if not isinstance(statement, (exp.Select, exp.Subqueryable if hasattr(exp, "Subqueryable") else exp.Select)):
        # WITH ... SELECT parses as exp.With wrapping a Select; allow that too.
        if not isinstance(statement, exp.With) and statement.find(exp.Select) is None:
            raise UnsafeQuery("Only SELECT statements are allowed.")

    for node_type in _FORBIDDEN:
        if statement.find(node_type) is not None:
            raise UnsafeQuery(f"{node_type.__name__.upper()} statements are not allowed.")

    # Every referenced table must exist in this workbook.
    for table in statement.find_all(exp.Table):
        name = table.name
        if name and name not in allowed_tables:
            raise UnsafeQuery(
                f'Unknown table "{name}". Available tables: {", ".join(sorted(allowed_tables))}'
            )

    # Column check is advisory: aliases and computed names are legitimate, so
    # only reject a bare identifier that matches nothing anywhere.
    referenced = {
        column.name
        for column in statement.find_all(exp.Column)
        if column.name and column.name != "*"
    }
    aliases = {alias.alias_or_name for alias in statement.find_all(exp.Alias)}
    unknown = referenced - allowed_columns - aliases
    if unknown:
        raise UnsafeQuery(
            f'Unknown column(s): {", ".join(sorted(unknown))}. '
            "Use only columns present in the schema."
        )

    # Enforce a row cap so a stray SELECT * cannot flood the context window.
    select = statement if isinstance(statement, exp.Select) else statement.find(exp.Select)
    if select is not None:
        limit = select.args.get("limit")
        if limit is None:
            statement = statement.limit(MAX_RESULT_ROWS)
        else:
            try:
                if int(limit.expression.name) > MAX_RESULT_ROWS:
                    statement = statement.limit(MAX_RESULT_ROWS)
            except (AttributeError, ValueError):
                statement = statement.limit(MAX_RESULT_ROWS)

    return statement.sql(dialect="duckdb")

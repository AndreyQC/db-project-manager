"""Text normalization helpers for the SQL parser.

Ported from POC dflw_parser_pg_sql.py (get_normalized_file_content,
get_object_name) with logging via loguru and no sys.path hacks.

The normalization is intentionally simplistic (whitespace + lower-case +
strip punctuation) because the parser scans for keyword pairs around
identifiers. A future sqlglot-based parser backend will replace this with
proper AST traversal.
"""

from __future__ import annotations

import re


def get_normalized_file_content(file_content: str) -> str:
    """Normalize SQL text for keyword/identifier scanning.

    Steps:
      1. Replace tabs/parens/brackets/semicolons/commas/quotes with spaces.
      2. Lower-case.
      3. Drop noise tokens: ``if not exists``, ``or replace``, ``external``.
      4. Collapse repeated spaces.
    """
    normalized = (
        file_content.replace("\t", " ")
        .replace("(", " ")
        .replace(")", " ")
        .replace("[", "")
        .replace("]", "")
        .replace(";", "")
        .replace(",", " ")
        .replace("\n", " ")
        .replace("'", " ")
        .replace('"', "")
        .lower()
    )
    normalized = (
        normalized.replace("if not exists", "").replace("or replace", "").replace("external", "")
    )
    normalized = re.sub(r" +", " ", normalized)
    return normalized


def get_object_name(qualified: str) -> dict[str, str]:
    """Parse a qualified SQL object name into schema/name/full_name.

    Examples:
        ``"bookings.aircrafts"`` -> {schema: "bookings", name: "aircrafts", full_name: "bookings.aircrafts"}
        ``"aircrafts"``          -> {schema: "public",    name: "aircrafts", full_name: "aircrafts"}
    """
    name = qualified.replace("[", "").replace("]", "").replace(";", "").strip()
    parts = name.split(".")
    if len(parts) == 1:
        return {"schema": "public", "name": parts[0], "full_name": parts[0]}
    schema, _, obj = name.rpartition(".")
    # rpartition handles 2+ dots by keeping the last segment as name; everything
    # before becomes the schema (rare, but stable).
    if "." in schema:
        schema = schema.rsplit(".", 1)[-1]
    return {"schema": schema, "name": obj, "full_name": f"{schema}.{obj}"}

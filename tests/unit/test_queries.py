"""Sanity checks for the catalog SQL query texts.

These are NOT execution tests (they need a live DB) — they guard against typos
in column names that surfaced at runtime before (e.g. atttypod vs atttypmod).
"""

from __future__ import annotations

import re

from db_project_manager.infrastructure.database.postgres import queries

#: Columns of pg_catalog.pg_attribute actually referenced via the owning_col alias.
#: Anything else here is almost certainly a typo. See PostgreSQL docs for pg_attribute.
_VALID_OWNING_COL_ATTRS = {"attname", "attnum", "attrelid", "atttypid", "atttypmod"}


def test_sequence_queries_use_correct_type_column() -> None:
    """Regression: atttypod (typo) was used instead of atttypmod, causing
    UndefinedColumn at runtime on get_sequences.
    """
    for query in (queries.GET_SEQUENCES_POSTGRES, queries.GET_SEQUENCES_GREENPLUM):
        assert "owning_col.atttypod" not in query, "typo atttypod reintroduced"
        assert "owning_col.atttypmod" in query


def test_owning_col_references_are_valid() -> None:
    """Every owning_col.<attr> in any query must be a known pg_attribute column."""
    text = queries.__doc__ or ""
    # Concatenate all query constants to scan them.
    all_queries = "\n".join(
        getattr(queries, name) for name in dir(queries) if name.isupper()
    )
    refs = re.findall(r"owning_col\.([a-z_]+)", all_queries)
    unknown = set(refs) - _VALID_OWNING_COL_ATTRS
    assert not unknown, f"unknown owning_col attributes (likely typos): {unknown}"
    # text var kept for clarity; unused otherwise.
    _ = text


def test_all_queries_are_nonempty_strings() -> None:
    names = [n for n in dir(queries) if n.isupper()]
    assert names, "no query constants found"
    for name in names:
        value = getattr(queries, name)
        assert isinstance(value, str) and value.strip(), f"{name} is empty"

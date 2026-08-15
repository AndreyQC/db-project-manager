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

#: Catalog columns per alias for the Phase 5 queries (guard against typos like
#: atttypod — the alias is only scanned inside its own query, since aliases such
#: as ``d`` mean pg_depend in the sequence queries and pg_database in the new ones).
_VALID_PG_EXTENSION_ATTRS = {
    "oid", "extname", "extowner", "extnamespace", "extrelocatable",
    "extversion", "extconfig", "extcondition",
}
_VALID_PG_NAMESPACE_ATTRS = {"oid", "nspname", "nspowner", "nspacl"}
_VALID_PG_DATABASE_ATTRS = {
    "oid", "datname", "datdba", "encoding", "datlocprovider", "datistemplate",
    "datallowconn", "dathasloginevt", "datconnlimit", "datfrozenxid",
    "datminmxid", "dattablespace", "datcollate", "datctype", "datlocale",
    "daticurules", "datcollversion", "datacl",
}
_VALID_PG_DB_ROLE_SETTING_ATTRS = {"setdatabase", "setrole", "setconfig"}

#: Phase 9: aliases in GET_TABLE_ROW_COUNTS — c = pg_class, n = pg_namespace.
_VALID_PG_CLASS_ATTRS = {
    "oid", "relname", "relnamespace", "relkind", "reltuples", "relpages",
    "relowner", "reltablespace", "relchecks", "relhasindex",
}
_VALID_PG_NAMESPACE_ROW_ATTRS = {"oid", "nspname", "nspowner", "nspacl"}


def _assert_alias_refs(query: str, alias_columns: dict[str, set[str]]) -> None:
    """Every ``alias.<attr>`` in *query* must be a known column of its catalog."""
    for alias, valid in alias_columns.items():
        refs = re.findall(rf"\b{alias}\.([a-z_]+)", query)
        unknown = set(refs) - valid
        assert not unknown, f"unknown {alias}.* attributes (likely typos): {unknown}"


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


def test_extension_query_uses_valid_catalog_columns() -> None:
    _assert_alias_refs(
        queries.GET_EXTENSIONS,
        {"e": _VALID_PG_EXTENSION_ATTRS, "n": _VALID_PG_NAMESPACE_ATTRS},
    )


def test_database_properties_query_uses_valid_catalog_columns() -> None:
    _assert_alias_refs(
        queries.GET_DATABASE_PROPERTIES, {"d": _VALID_PG_DATABASE_ATTRS}
    )
    # Behaviour-relevant fields only (Phase 5 vision Q8).
    for field in ("encoding", "datcollate", "datctype"):
        assert field in queries.GET_DATABASE_PROPERTIES
    for excluded in ("datconnlimit", "datistemplate", "datallowconn"):
        assert excluded not in queries.GET_DATABASE_PROPERTIES


def test_database_settings_query_uses_valid_catalog_columns() -> None:
    _assert_alias_refs(
        queries.GET_DATABASE_SETTINGS,
        {"s": _VALID_PG_DB_ROLE_SETTING_ATTRS, "d": _VALID_PG_DATABASE_ATTRS},
    )


def test_database_settings_query_filters_role_level() -> None:
    """Phase 5 vision Q5: only db-level settings (setrole = 0) are carried over."""
    assert "s.setrole = 0" in queries.GET_DATABASE_SETTINGS


def test_table_row_counts_query_uses_valid_catalog_columns() -> None:
    """Phase 9: guard against typos in pg_class/pg_namespace column names."""
    _assert_alias_refs(
        queries.GET_TABLE_ROW_COUNTS,
        {"c": _VALID_PG_CLASS_ATTRS, "n": _VALID_PG_NAMESPACE_ROW_ATTRS},
    )
    # Filters: only base tables, excluding system schemas.
    assert "relkind = 'r'" in queries.GET_TABLE_ROW_COUNTS
    assert "pg_catalog" in queries.GET_TABLE_ROW_COUNTS
    assert "information_schema" in queries.GET_TABLE_ROW_COUNTS
    # The estimated-rows column must be selected.
    assert "reltuples" in queries.GET_TABLE_ROW_COUNTS


def test_function_query_excludes_extension_owned() -> None:
    """Regression: functions owned by an extension (deptype='e' in pg_depend)
    must be filtered out — they are created via CREATE EXTENSION, and emitting
    them as user objects causes 'cannot change name of input parameter' on
    deploy (the extension's own definition has different arg names).
    """
    assert "deptype = 'e'" in queries.GET_FUNCTIONS


def test_procedure_query_excludes_extension_owned() -> None:
    """Same extension-ownership filter as for functions (see above)."""
    assert "deptype = 'e'" in queries.GET_PROCEDURES

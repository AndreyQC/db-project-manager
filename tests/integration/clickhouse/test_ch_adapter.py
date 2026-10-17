"""Integration tests for CHDatabaseAdapter.

Live CH cluster (or CLICKHOUSE_TEST_* env vars). Each test gets a fresh
per-test database, created and dropped by the ``ch_conn_cfg`` fixture.
Marked ``clickhouse`` via ``conftest.py`` — run with
``uv run pytest -m clickhouse``.
"""

from __future__ import annotations

import datetime as dt

import pytest

from db_project_manager.domain.connection import ConnectionConfig
from db_project_manager.domain.deploy import ScriptRecord
from db_project_manager.domain.safety import StatsConfidence
from db_project_manager.infrastructure.database.registry import get_adapter


# --------------------------------------------------------------------------- #
# connect / disconnect
# --------------------------------------------------------------------------- #

def test_connect_disconnect(ch_conn_cfg: ConnectionConfig) -> None:
    """connect() + disconnect() round-trip without errors."""
    a = get_adapter(ch_conn_cfg)
    assert a._client is None  # not connected yet
    a.connect(ch_conn_cfg)
    assert a._client is not None
    a.disconnect()
    assert a._client is None


def test_connect_invalid_host_raises(ch_conn_cfg: ConnectionConfig) -> None:
    """Connecting to an unreachable host must raise DatabaseError, not raw exception."""
    from db_project_manager.infrastructure.database.base import DatabaseError

    bad = ch_conn_cfg.model_copy(update={"host": "127.0.0.1", "port": 1})
    a = get_adapter(bad)
    with pytest.raises(DatabaseError):
        a.connect(bad)


# --------------------------------------------------------------------------- #
# RE: get_database_structure
# --------------------------------------------------------------------------- #

def _per_test_schema(structure: dict, db_name: str) -> dict:
    """Helper: pick the schema entry for the per-test DB out of a
    full get_database_structure() result (which lists ALL user DBs)."""
    schemas = {s["name"]: s for s in structure["schemas"]}
    assert db_name in schemas, (
        f"per-test DB {db_name!r} not in structure.schemas: "
        f"{list(schemas)}"
    )
    return schemas[db_name]


def test_re_empty_database(ch_conn_cfg: ConnectionConfig) -> None:
    """A fresh per-test DB has 0 user tables / views / dicts / mutations."""
    a = get_adapter(ch_conn_cfg)
    a.connect(ch_conn_cfg)
    try:
        s = a.get_database_structure()
    finally:
        a.disconnect()

    schema = _per_test_schema(s, ch_conn_cfg.database)
    assert schema["tables"] == []
    assert schema["views"] == []
    assert schema["materialized_views"] == []
    assert schema["dictionaries"] == []
    assert schema["pending_mutations"] == []


def test_re_after_create_table(ch_conn_cfg: ConnectionConfig) -> None:
    """After CREATE TABLE, RE surfaces the table with engine, columns, sort key."""
    a = get_adapter(ch_conn_cfg)
    a.connect(ch_conn_cfg)
    try:
        a.execute_script(
            f"CREATE TABLE {ch_conn_cfg.database}.foo ("
            "id UInt64, name String, score Decimal(10,2)"
            ") ENGINE = MergeTree ORDER BY id"
        )
        s = a.get_database_structure()
    finally:
        a.disconnect()

    schema = _per_test_schema(s, ch_conn_cfg.database)
    assert len(schema["tables"]) == 1
    foo = schema["tables"][0]
    assert foo["name"] == "foo"
    assert foo["engine"] == "MergeTree"
    assert foo["sorting_key"] == "id"
    assert foo["partition_key"] == ""
    assert [c["name"] for c in foo["columns"]] == ["id", "name", "score"]
    assert foo["columns"][2]["type"] == "Decimal(10, 2)" or foo["columns"][2]["type"] == "Decimal(10,2)"
    # storage_stats present (even if 0)
    assert "active_parts" in foo["storage_stats"]
    assert "parts_total_bytes" in foo["storage_stats"]
    # skip_indexes empty
    assert foo["skip_indexes"] == []


def test_re_views_and_mvs(ch_conn_cfg: ConnectionConfig) -> None:
    """RE separates regular views and materialized views.

    MaterializedView entries also carry the source table(s) in
    ``dependencies`` (resolved from system.tables.dependencies_database /
    dependencies_table) — the graph layer can use this to draw MV→table
    edges without parsing the DDL."""
    a = get_adapter(ch_conn_cfg)
    a.connect(ch_conn_cfg)
    try:
        db = ch_conn_cfg.database
        a.execute_script(
            f"CREATE TABLE {db}.base (id UInt64, val String) "
            "ENGINE = MergeTree ORDER BY id"
        )
        a.execute_script(
            f"CREATE VIEW {db}.v1 AS SELECT id, val FROM {db}.base"
        )
        a.execute_script(
            f"CREATE MATERIALIZED VIEW {db}.mv1 "
            f"ENGINE = MergeTree ORDER BY id AS "
            f"SELECT id, val FROM {db}.base"
        )
        s = a.get_database_structure()
    finally:
        a.disconnect()

    schema = _per_test_schema(s, ch_conn_cfg.database)
    assert len(schema["tables"]) == 1
    assert len(schema["views"]) == 1
    assert len(schema["materialized_views"]) == 1
    assert schema["views"][0]["name"] == "v1"
    assert schema["materialized_views"][0]["name"] == "mv1"

    # MV: the structured ``dependencies`` field is present (CH populates
    # it from system.tables.dependencies_database / dependencies_table).
    # Note: CH only tracks EXTERNAL dependencies (cross-DB / dictionaries).
    # For a simple MV pulling from a same-DB table, this is empty; the
    # source can still be recovered from ``definition`` (as_select).
    mv = schema["materialized_views"][0]
    assert "dependencies" in mv
    assert isinstance(mv["dependencies"], list)
    # has_own_data: 0 for MVs with no rows yet (inner table not created);
    # 1 once the inner table is materialized. We don't assert either way
    # here — it depends on whether data was inserted.
    assert "has_own_data" in mv
    assert isinstance(mv["has_own_data"], bool)
    assert "base" in mv["definition"]  # as_select mentions 'base' (the source)


# --------------------------------------------------------------------------- #
# Phase 9 / 11: get_table_row_counts + get_table_presence_stats
# --------------------------------------------------------------------------- #

def test_get_table_presence_stats_does_not_crash(ch_conn_cfg: ConnectionConfig) -> None:
    """Regression: get_table_presence_stats used to crash on StatsConfidence.LOW.

    With one table in the per-test DB, the per-test table row appears with
    confidence=UNKNOWN (CH has no freshness signal, fail-safe default).
    Other user DBs may also appear in the result; we filter to ours."""
    a = get_adapter(ch_conn_cfg)
    a.connect(ch_conn_cfg)
    try:
        a.execute_script(
            f"CREATE TABLE {ch_conn_cfg.database}.foo (id UInt64) "
            "ENGINE = MergeTree ORDER BY id"
        )
        rows = a.get_table_presence_stats()
    finally:
        a.disconnect()

    our_rows = [
        r for r in rows
        if r.object_schema == ch_conn_cfg.database
    ]
    assert len(our_rows) == 1
    foo = our_rows[0]
    assert foo.name == "foo"
    assert foo.estimated_rows == 0
    assert foo.confidence == StatsConfidence.UNKNOWN


# --------------------------------------------------------------------------- #
# Phase 10: deploy journal
# --------------------------------------------------------------------------- #

def test_deploy_journal_roundtrip(ch_conn_cfg: ConnectionConfig) -> None:
    """Schema version + script history UPSERT round-trip end-to-end."""
    a = get_adapter(ch_conn_cfg)
    a.connect(ch_conn_cfg)
    try:
        target = ch_conn_cfg.database

        # Initial: no version
        assert a.get_schema_version(target) is None

        # Record v1
        a.record_schema_version(target, "2026.10.17.01", "manual")
        assert a.get_schema_version(target) == "2026.10.17.01"

        # Record v2 — latest wins
        a.record_schema_version(target, "2026.10.17.02", "deploy")
        assert a.get_schema_version(target) == "2026.10.17.02"

        # script_history: empty
        assert (
            a.get_script_history(target, "foo", "pre") is None
        )

        # Record a successful run
        rec = ScriptRecord(
            script_name="foo",
            script_type="pre",
            checksum="abc123",
            success=True,
            executed_at=dt.datetime.now(dt.timezone.utc),
            error_message=None,
            duration_ms=42,
        )
        a.record_script_execution(target, rec, "2026.10.17.02", "deploy")

        # Read back
        hist = a.get_script_history(target, "foo", "pre")
        assert hist is not None
        assert hist.script_name == "foo"
        assert hist.success is True
        assert hist.duration_ms == 42
        assert hist.error_message is None

        # UPSERT: re-run with failure — replaces the row
        rec_fail = ScriptRecord(
            script_name="foo",
            script_type="pre",
            checksum="abc123",
            success=False,
            executed_at=dt.datetime.now(dt.timezone.utc),
            error_message="boom",
            duration_ms=7,
        )
        a.record_script_execution(target, rec_fail, "2026.10.17.02", "deploy")

        hist2 = a.get_script_history(target, "foo", "pre")
        assert hist2 is not None
        assert hist2.success is False
        assert hist2.error_message == "boom"
        assert hist2.duration_ms == 7
    finally:
        a.disconnect()


# --------------------------------------------------------------------------- #
# DDL: create_database / drop_database (ON CLUSTER)
# --------------------------------------------------------------------------- #

def test_create_and_drop_database(ch_admin_conn_cfg: ConnectionConfig) -> None:
    """create_database / drop_database work via the cluster."""
    a = get_adapter(ch_admin_conn_cfg)
    a.connect(ch_admin_conn_cfg)
    try:
        name = f"ch_ddl_{__import__('uuid').uuid4().hex[:8]}"
        a.create_database(name)
        # The DB must appear in the visible user-DB list
        assert name in a.list_schemas()
        a.drop_database(name)
        assert name not in a.list_schemas()
    finally:
        a.disconnect()


def test_drop_schema_contents(ch_conn_cfg: ConnectionConfig) -> None:
    """drop_schema_contents empties the DB shell but keeps the DB itself.

    Creates a mix of objects (table, view, MV) plus a real DICTIONARY
    (CLICKHOUSE source pointing at the test's own table — CH does not
    allow SOURCE(NULL) for dictionaries), then drops everything. The DB
    must still exist, but contain no user objects."""
    a = get_adapter(ch_conn_cfg)
    a.connect(ch_conn_cfg)
    try:
        db = ch_conn_cfg.database
        a.execute_script(
            f"CREATE TABLE {db}.base (id UInt64, name String) "
            "ENGINE = MergeTree ORDER BY id"
        )
        a.execute_script(
            f"CREATE VIEW {db}.v1 AS SELECT id FROM {db}.base"
        )
        a.execute_script(
            f"CREATE MATERIALIZED VIEW {db}.mv1 "
            f"ENGINE = MergeTree ORDER BY id AS "
            f"SELECT id FROM {db}.base"
        )
        # Real DICTIONARY — CLICKHOUSE source pointing at the test's own
        # table. CH has no SOURCE(NULL) for dictionaries; lifetime is
        # mandatory (LIFETIME(MIN 60 MAX 300) = reload every 1..5 min).
        a.execute_script(
            f"CREATE DICTIONARY {db}.d1 ("
            "id UInt64, name String"
            ") PRIMARY KEY id "
            "LAYOUT(FLAT()) "
            f"SOURCE(CLICKHOUSE(DB '{db}' TABLE 'base')) "
            "LIFETIME(MIN 60 MAX 300)"
        )

        # Pre-condition: schema has objects
        s = a.get_database_structure()
        schema = _per_test_schema(s, ch_conn_cfg.database)
        assert len(schema["tables"]) >= 1
        assert len(schema["views"]) >= 1
        assert len(schema["materialized_views"]) >= 1
        assert len(schema["dictionaries"]) >= 1

        # Drop everything
        a.drop_schema_contents(ch_conn_cfg.database)

        # Post-condition: schema is empty but still exists
        s2 = a.get_database_structure()
        schema2 = _per_test_schema(s2, ch_conn_cfg.database)
        assert schema2["tables"] == []
        assert schema2["views"] == []
        assert schema2["materialized_views"] == []
        assert schema2["dictionaries"] == []
    finally:
        a.disconnect()


def test_re_real_dictionary(ch_conn_cfg: ConnectionConfig) -> None:
    """A real DICTIONARY (LAYOUT(FLAT) + CLICKHOUSE source) appears under
    RE's ``dictionaries[]`` group, separate from regular tables.

    Note: ``system.dictionaries.type`` (the layout type like "FLAT" /
    "hashed") is only populated AFTER the dictionary has been loaded for
    the first time (via dictGet() or SYSTEM RELOAD DICTIONARY). For
    un-loaded dicts it's an empty string. The test only asserts the
    structural fields that are always populated."""
    a = get_adapter(ch_conn_cfg)
    a.connect(ch_conn_cfg)
    try:
        db = ch_conn_cfg.database
        a.execute_script(
            f"CREATE TABLE {db}.base (id UInt64, name String) "
            "ENGINE = MergeTree ORDER BY id"
        )
        a.execute_script(
            f"CREATE DICTIONARY {db}.country_dict ("
            "id UInt64, name String"
            ") PRIMARY KEY id "
            "LAYOUT(FLAT()) "
            f"SOURCE(CLICKHOUSE(DB '{db}' TABLE 'base')) "
            "LIFETIME(MIN 60 MAX 300)"
        )
        s = a.get_database_structure()
    finally:
        a.disconnect()

    schema = _per_test_schema(s, ch_conn_cfg.database)
    # The base table is a regular table, not a dictionary
    assert len(schema["tables"]) == 1
    assert schema["tables"][0]["name"] == "base"
    # The dictionary is in the dictionaries list, not the tables list
    assert len(schema["dictionaries"]) == 1
    d = schema["dictionaries"][0]
    assert d["name"] == "country_dict"
    # ``origin`` is the path to the dict config (set by the DDL we issued)
    assert d["origin"]  # non-empty (typically "default.country_dict")
    # ``type`` may be empty (not yet loaded) or "FLAT"/"flat" (loaded) —
    # we just verify the field exists, not its value
    assert "type" in d
    # ``source`` is the free-form text describing the source
    assert "source" in d


# --------------------------------------------------------------------------- #
# Phase 19 MCP: run_query
# --------------------------------------------------------------------------- #

def test_run_query_basic(ch_conn_cfg: ConnectionConfig) -> None:
    """run_query returns rows as dicts with column names."""
    a = get_adapter(ch_conn_cfg)
    a.connect(ch_conn_cfg)
    try:
        r = a.run_query(
            "SELECT 1 AS n, 'hello' AS s",
            max_rows=10,
        )
        assert r.columns == ["n", "s"]
        assert len(r.rows) == 1
        assert r.rows[0] == {"n": 1, "s": "hello"}
        assert r.truncated is False
        assert r.row_count == 1
    finally:
        a.disconnect()

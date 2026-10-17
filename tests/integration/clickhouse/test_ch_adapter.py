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
    """RE separates regular views and materialized views."""
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

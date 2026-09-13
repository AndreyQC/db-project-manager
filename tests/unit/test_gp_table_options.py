"""Greenplum table properties in RE: DISTRIBUTED + WITH storage (Phase 16.6).

Live-probe facts (cis_zup_gp_dev, GP 6.19.4): every table has a
gp_distribution_policy row; 'p' + empty distkey = DISTRIBUTED RANDOMLY
(196/196 on this cluster), 'p' + distkey = DISTRIBUTED BY (cols), 'r' =
DISTRIBUTED REPLICATED. PostgreSQL has no gp_distribution_policy — the
options map is {} there and the render is the plain pre-16.6 form.
"""

from __future__ import annotations

from db_project_manager.infrastructure.database.postgres import queries as q
from db_project_manager.infrastructure.database.postgres.adapter import PGDatabaseAdapter
from db_project_manager.infrastructure.sql.sql_generator import gp_tail_sql


def _make_adapter(monkeypatch, *, is_greenplum: bool, gp_rows: list | None = None):
    adapter = PGDatabaseAdapter()
    adapter._is_greenplum = is_greenplum

    def fake_exec(query, params=None):
        if query == q.GET_TABLE_GP_OPTIONS:
            return gp_rows or []
        return []

    monkeypatch.setattr(adapter, "_exec", fake_exec)
    return adapter


def test_gp_options_map_parsed_per_policytype(monkeypatch) -> None:
    rows = [
        ("t_random", "p", None, ["appendoptimized=true", "orientation=column"]),
        ("t_by", "p", "process_log_id", None),
        ("t_repl", "r", None, None),
    ]
    adapter = _make_adapter(monkeypatch, is_greenplum=True, gp_rows=rows)

    options = adapter._get_gp_table_options("schema_a")

    assert options["t_random"]["distribution"] == {"kind": "randomly", "columns": []}
    assert options["t_random"]["storage_options"] == ["appendoptimized=true", "orientation=column"]
    assert options["t_by"]["distribution"] == {"kind": "by", "columns": ["process_log_id"]}
    assert options["t_by"]["storage_options"] is None
    assert options["t_repl"]["distribution"] == {"kind": "replicated", "columns": []}


def test_gp_options_multi_column_distkey_split(monkeypatch) -> None:
    adapter = _make_adapter(
        monkeypatch, is_greenplum=True, gp_rows=[("t", "p", "a, b", None)]
    )
    options = adapter._get_gp_table_options("schema_a")
    assert options["t"]["distribution"] == {"kind": "by", "columns": ["a", "b"]}


def test_postgres_connection_skips_gp_options_query(monkeypatch) -> None:
    adapter = _make_adapter(monkeypatch, is_greenplum=False, gp_rows=[("t", "p", None, None)])

    assert adapter._get_gp_table_options("schema_a") == {}


def test_build_table_carries_gp_options(monkeypatch) -> None:
    adapter = _make_adapter(
        monkeypatch, is_greenplum=True, gp_rows=[("t", "p", None, ["orientation=column"])]
    )
    monkeypatch.setattr(
        adapter,
        "_exec",
        lambda query, params=None: [],
    )
    gp = {"distribution": {"kind": "randomly", "columns": []}, "storage_options": ["orientation=column"]}
    table = adapter._build_table("schema_a", {"name": "t", "comment": None}, gp)

    assert table["distribution"] == {"kind": "randomly", "columns": []}
    assert table["storage_options"] == ["orientation=column"]


def test_gp_tail_sql_renders_all_kinds() -> None:
    assert gp_tail_sql({}) == ""
    assert gp_tail_sql({"distribution": None, "storage_options": None}) == ""
    assert (
        gp_tail_sql({"distribution": {"kind": "randomly", "columns": []}})
        == "\nDISTRIBUTED RANDOMLY"
    )
    assert (
        gp_tail_sql({"distribution": {"kind": "replicated", "columns": []}})
        == "\nDISTRIBUTED REPLICATED"
    )
    assert (
        gp_tail_sql({"distribution": {"kind": "by", "columns": ["a", "b"]}})
        == '\nDISTRIBUTED BY ("a", "b")'
    )
    assert (
        gp_tail_sql(
            {
                "storage_options": ["appendoptimized=true", "orientation=column"],
                "distribution": {"kind": "randomly", "columns": []},
            }
        )
        == "\nWITH (appendoptimized=true, orientation=column)\nDISTRIBUTED RANDOMLY"
    )


def test_gp_options_query_avoids_pg95_constructs() -> None:
    """LESSONS §70: the kernel is PG 9.4 — no array_position (the GET_INDEXES
    regression). distkey attnames resolve via unnest WITH ORDINALITY."""
    assert "array_position" not in q.GET_TABLE_GP_OPTIONS
    assert "WITH ORDINALITY" in q.GET_TABLE_GP_OPTIONS
    assert "pg_catalog.gp_distribution_policy" in q.GET_TABLE_GP_OPTIONS


def test_gp_options_query_one_per_schema_not_per_table() -> None:
    """The map is fetched once per schema (9 queries per RE run), not per
    table (196) — mirrors the GET_TABLES flow."""
    assert "c.relname AS table_name" in q.GET_TABLE_GP_OPTIONS
    assert ":schema" in q.GET_TABLE_GP_OPTIONS


# --- serial detection (Phase 16.7, LESSONS §67 family) ---


def _make_columns_adapter(monkeypatch, columns_rows):
    adapter = PGDatabaseAdapter()
    adapter._is_greenplum = True

    def fake_exec(query, params=None):
        if query == q.GET_COLUMNS:
            return columns_rows
        return []

    monkeypatch.setattr(adapter, "_exec", fake_exec)
    return adapter


def test_default_named_sequence_column_renders_serial(monkeypatch) -> None:
    adapter = _make_columns_adapter(
        monkeypatch,
        [
            ("id", "int4", "NO", "nextval('s1.t_id_seq'::regclass)", None, 32, 0, None),
            ("code", "int8", "NO", "nextval('s1.t_code_seq'::regclass)", None, 64, 0, None),
        ],
    )
    table = adapter._build_table("s1", {"name": "t", "comment": None})
    assert [c["type"] for c in table["columns"]] == ["serial4", "serial8"]
    assert all(c["default"] is None for c in table["columns"])


def test_custom_named_sequence_keeps_default(monkeypatch) -> None:
    adapter = _make_columns_adapter(
        monkeypatch,
        [("id", "int4", "NO", "nextval('s1.custom_seq'::regclass)", None, 32, 0, None)],
    )
    table = adapter._build_table("s1", {"name": "t", "comment": None})
    assert table["columns"][0]["type"] == "int4"
    assert "custom_seq" in table["columns"][0]["default"]


def test_serial4_in_no_numeric_mod_set() -> None:
    """serial4 must not get a bogus (32, 0) type modifier (regression of the
    2026-09-13 live run)."""
    from db_project_manager.infrastructure.sql.sql_generator import _NO_NUMERIC_MOD

    assert {"serial4", "serial8", "smallserial"} <= _NO_NUMERIC_MOD

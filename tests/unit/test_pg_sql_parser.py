"""Tests for db_project_manager.infrastructure.parsing.pg_sql_parser.

Uses tests/fixtures/codebase_sample/ — a small synthetic codebase with
predictable dependencies (FK, JOIN, nextval) plus a build:false object.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from db_project_manager.domain.graph import Relation
from db_project_manager.infrastructure.parsing.normalize import (
    get_normalized_file_content,
    get_object_name,
)
from db_project_manager.infrastructure.parsing.pg_sql_parser import PgSqlParser

FIXTURE_ROOT = Path(__file__).resolve().parent.parent / "fixtures" / "codebase_sample"

# object_keys used in assertions
AIRCRAFTS = "pg_database/demo/schema/bookings/type/table/name/aircrafts"
AIRPORTS = "pg_database/demo/schema/bookings/type/table/name/airports"
FLIGHTS = "pg_database/demo/schema/bookings/type/table/name/flights"
TICKETS = "pg_database/demo/schema/bookings/type/table/name/tickets"
TICKETS_SEQ = "pg_database/demo/schema/bookings/type/sequence/name/tickets_id_seq"
FLIGHTS_V = "pg_database/demo/schema/bookings/type/view/name/flights_v"
ROUTES = "pg_database/demo/schema/bookings/type/materialized_view/name/routes"


@pytest.fixture(scope="module")
def graph():
    return PgSqlParser().parse_directory(FIXTURE_ROOT)


# --- normalize helpers ---


def test_get_object_name_qualified() -> None:
    parsed = get_object_name("bookings.aircrafts")
    assert parsed == {"schema": "bookings", "name": "aircrafts", "full_name": "bookings.aircrafts"}


def test_get_object_name_bare() -> None:
    parsed = get_object_name("aircrafts")
    assert parsed == {"schema": "public", "name": "aircrafts", "full_name": "aircrafts"}


def test_normalized_content_lowercases_and_collapses_spaces() -> None:
    out = get_normalized_file_content("CREATE  TABLE\tFoo(x)")
    assert out == "create table foo x "
    # Noise tokens dropped.
    assert "if not exists" not in get_normalized_file_content("CREATE TABLE IF NOT EXISTS t(x)")


# --- vertices ---


def test_all_expected_vertices_present(graph) -> None:
    keys = set(graph.vertices.keys())
    assert {AIRCRAFTS, AIRPORTS, FLIGHTS, TICKETS, TICKETS_SEQ, FLIGHTS_V, ROUTES}.issubset(keys)


def test_vertex_identity_from_autodoc(graph) -> None:
    v = graph.get_vertex(FLIGHTS)
    assert v is not None
    assert v.object_type == "table"
    assert v.object_schema == "bookings"
    assert v.object_name == "flights"
    assert v.object_catalog == "demo"
    assert v.build is True
    # source file is relative to the codebase root
    assert v.object_source_file.endswith("bookings/tables/table flights.sql")


def test_build_false_vertex_present_in_graph(graph) -> None:
    """Q8: build:false vertices stay in the graph (filtered only at deploy)."""
    v = graph.get_vertex(ROUTES)
    assert v is not None
    assert v.build is False
    assert v.object_type == "materialized_view"


def test_supported_object_types() -> None:
    types = PgSqlParser().supported_object_types()
    assert "table" in types
    assert "view" in types
    assert "sequence" in types


# --- Phase 4: overloaded functions/procedures ---


# Two overloads of sp_x: distinct object_keys via /signature/<hash> suffix.
SP_X_INT = "pg_database/demo/schema/app/type/function/name/sp_x/signature/19f12f3f"
SP_X_TEXT = "pg_database/demo/schema/app/type/function/name/sp_x/signature/982d9e3e"
SP_Y = "pg_database/demo/schema/app/type/function/name/sp_y/signature/75666699"


def test_overloaded_functions_produce_distinct_vertices(graph) -> None:
    """The core Phase 4 fix: two functions named sp_x with different signatures
    must coexist in the graph instead of one silently overwriting the other
    (the old bug — both shared the same object_key)."""
    assert SP_X_INT in graph.vertices
    assert SP_X_TEXT in graph.vertices
    assert SP_X_INT != SP_X_TEXT


def test_overloaded_functions_share_object_name(graph) -> None:
    """Both overloads have the same object_name 'sp_x' — they differ only by signature."""
    v_int = graph.get_vertex(SP_X_INT)
    v_text = graph.get_vertex(SP_X_TEXT)
    assert v_int.object_name == "sp_x"
    assert v_text.object_name == "sp_x"
    assert v_int.object_signature == "19f12f3f"
    assert v_text.object_signature == "982d9e3e"


def test_singleton_function_vertex(graph) -> None:
    """A singleton function sp_y(uuid) — no overload siblings — is parsed normally
    and gets its /signature/<hash> key (deterministic identity)."""
    v = graph.get_vertex(SP_Y)
    assert v is not None
    assert v.object_name == "sp_y"
    assert v.object_signature == "75666699"
    assert v.object_type == "function"


def test_function_source_file_uses_short_or_sha_name(graph) -> None:
    """object_source_file matches the actual file name on disk: overloaded files
    carry the __<hash> suffix, singleton files keep the short name."""
    v_int = graph.get_vertex(SP_X_INT)
    v_text = graph.get_vertex(SP_X_TEXT)
    v_y = graph.get_vertex(SP_Y)
    assert v_int.object_source_file.endswith("app/functions/function sp_x__19f12f3f.sql")
    assert v_text.object_source_file.endswith("app/functions/function sp_x__982d9e3e.sql")
    assert v_y.object_source_file.endswith("app/functions/function sp_y.sql")


def test_overloads_survive_filter_build_true(graph) -> None:
    """Both overloads have build=true and must survive deploy filtering."""
    filtered = graph.filter_build_true()
    assert SP_X_INT in filtered.vertices
    assert SP_X_TEXT in filtered.vertices
    assert SP_Y in filtered.vertices


def test_overloads_use_posix_relative_source_path(graph) -> None:
    """Regression for LESSONS_LEARNED §20: source paths are forward-slash
    everywhere (no backslashes on Windows)."""
    for key in (SP_X_INT, SP_X_TEXT, SP_Y):
        path = graph.get_vertex(key).object_source_file
        assert "\\" not in path
        assert "/" in path


# --- edges: foreign keys (REFERENCES_BY) ---


def test_fk_edges_detected(graph) -> None:
    """flights -> aircrafts and flights -> airports (REFERENCES_BY)."""
    deps = {e.destination_object_key: e for e in graph.get_dependencies(FLIGHTS)}
    assert AIRCRAFTS in deps
    assert deps[AIRCRAFTS].relation == Relation.REFERENCES_BY
    assert deps[AIRCRAFTS].action == "references"
    # airports referenced by 3 FK columns but dedup keeps one edge per (src,dst,rel,action)
    assert AIRPORTS in deps


def test_fk_tickets_to_flights(graph) -> None:
    deps = {e.destination_object_key for e in graph.get_dependencies(TICKETS)}
    assert FLIGHTS in deps


def test_fk_deduplication_for_repeated_reference(graph) -> None:
    """flights references airports three times (departure/arrival/fk) — one edge."""
    airport_edges = [
        e for e in graph.edges
        if e.source_object_key == FLIGHTS and e.destination_object_key == AIRPORTS
    ]
    assert len(airport_edges) == 1


# --- edges: sequence nextval ---


def test_nextval_edge(graph) -> None:
    """tickets uses nextval(tickets_id_seq) -> SEQUENCE_NEXTVAL_IN."""
    seq_edges = [
        e for e in graph.get_dependencies(TICKETS)
        if e.relation == Relation.SEQUENCE_NEXTVAL_IN
    ]
    assert len(seq_edges) == 1
    assert seq_edges[0].destination_object_key == TICKETS_SEQ


# --- edges: view joins ---


def test_view_select_and_join_edges(graph) -> None:
    """flights_v: SELECT from flights (PROVIDE_DATA_TO) and LEFT JOIN aircrafts."""
    deps = graph.get_dependencies(FLIGHTS_V)
    dests = {(e.destination_object_key, e.relation) for e in deps}
    # SELECT from flights
    assert (FLIGHTS, Relation.PROVIDE_DATA_TO) in dests
    # LEFT JOIN aircrafts (Q1 fix: 'left join' must not be swallowed by generic 'join')
    aircrafts_edges = [e for e in deps if e.destination_object_key == AIRCRAFTS]
    assert aircrafts_edges
    assert any("left join" in e.action for e in aircrafts_edges)


def test_left_join_classified_correctly(graph) -> None:
    """Regression for POC bug: 'left join' was unreachable after generic 'join'."""
    deps = [e for e in graph.edges if e.source_object_key == FLIGHTS_V and e.destination_object_key == AIRCRAFTS]
    assert deps, "expected flights_v -> aircrafts edge via LEFT JOIN"
    assert deps[0].action == "select left join"


# --- edges: build:false object still scanned ---


def test_build_false_object_edges_kept(graph) -> None:
    """routes (build:false) is scanned; its edges are kept too."""
    deps = {e.destination_object_key for e in graph.get_dependencies(ROUTES)}
    assert FLIGHTS in deps


# --- graph invariants ---


def test_no_self_edges(graph) -> None:
    for e in graph.edges:
        assert e.source_object_key != e.destination_object_key


def test_edge_endpoints_resolve(graph) -> None:
    """All edges point to known vertices (no dangling references in fixture)."""
    assert graph.dangling_edges() == []


# --- Phase 5: extensions and database_settings ---


EXT_KEY = "pg_database/demo/type/extension/name/citext"
DB_SET_KEY = "pg_database/demo/type/database_setting/name/database settings"


def test_extension_vertex_schema_less(graph) -> None:
    """extension is schema-less (object_schema=None); key has no schema/ segment."""
    v = graph.get_vertex(EXT_KEY)
    assert v is not None
    assert v.object_schema is None
    assert v.object_type == "extension"
    assert v.object_name == "citext"
    assert v.object_source_file.endswith("extensions/extension citext.sql")


def test_database_setting_vertex_schema_less(graph) -> None:
    """database_setting is schema-less; carries db_properties from autodoc."""
    v = graph.get_vertex(DB_SET_KEY)
    assert v is not None
    assert v.object_schema is None
    assert v.object_type == "database_setting"
    assert v.extra is not None
    assert v.extra.get("db_properties") == {
        "encoding": "UTF8",
        "lc_collate": "C",
        "lc_ctype": "C",
        "template": "template0",
    }


def test_extension_and_database_setting_supported_types() -> None:
    types = PgSqlParser().supported_object_types()
    assert "extension" in types
    assert "database_setting" in types


# --- Phase 6 follow-up: function-call edges (DEPENDS_ON/call) ---


SP_CALLER = "pg_database/demo/schema/app/type/function/name/sp_caller/signature/75666699"


def test_function_call_in_case_when_creates_depends_on_edge(graph) -> None:
    """Regression: a function calling another inside CASE WHEN (or any non-FROM
    context) must produce a DEPENDS_ON edge. Previously missed because
    _classify_at only handled FK/nextval/JOIN/DML/SELECT."""
    # sp_caller exists.
    assert SP_CALLER in graph.vertices
    # Edge: sp_caller -> sp_y with DEPENDS_ON / action="call".
    call_edges = [
        e for e in graph.edges
        if e.source_object_key == SP_CALLER and e.destination_object_key == SP_Y
    ]
    assert call_edges, "expected DEPENDS_ON edge sp_caller -> sp_y, got none"
    assert all(e.relation == Relation.DEPENDS_ON for e in call_edges)
    assert all(e.action == "call" for e in call_edges)


def test_function_call_edge_count(graph) -> None:
    """At least one function-call edge exists in the fixture after the fix."""
    call_edges = [e for e in graph.edges if e.action == "call"]
    assert call_edges, "no DEPENDS_ON/call edges detected (regression)"


def test_extension_file_inside_skip_dirs_ignored(tmp_path) -> None:
    """Files inside .dbm_graph/.git/etc. are skipped (shared with other types)."""
    parser = PgSqlParser()
    # Explicitly create .dbm_graph subdir (tmp_path may not auto-create nested dirs on Windows).
    dbm = tmp_path / ".dbm_graph"
    dbm.mkdir()
    (dbm / "ext.sql").write_text("CREATE EXTENSION citext", encoding="utf-8")
    result = parser.parse_directory(tmp_path)
    assert "pg_database" not in result.vertices

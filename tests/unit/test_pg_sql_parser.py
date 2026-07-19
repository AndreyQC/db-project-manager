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

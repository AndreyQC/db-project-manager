"""Tests for db_project_manager.domain.graph (pure model, no I/O)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from db_project_manager.domain.graph import (
    CycleError,
    DependencyGraph,
    Edge,
    Relation,
    Vertex,
)


def _vertex(key: str, *, object_type: str = "table", build: bool = True) -> Vertex:
    """Helper: build a Vertex with a minimal but valid object_key."""
    # Key shape: pg_database/<catalog>/schema/<schema>/type/<type>/name/<name>
    return Vertex(
        object_key=key,
        object_catalog="db",
        object_schema="bookings",
        object_type=object_type,
        object_name=key.rsplit("/", 1)[-1],
        object_source_file=f"src/{key}.sql",
        build=build,
    )


AIRCRAFTS = "pg_database/db/schema/bookings/type/table/name/aircrafts"
FLIGHTS = "pg_database/db/schema/bookings/type/table/name/flights"
TICKETS = "pg_database/db/schema/bookings/type/table/name/tickets"
SEQ = "pg_database/db/schema/bookings/type/sequence/name/tickets_id_seq"
FLIGHTS_V = "pg_database/db/schema/bookings/type/view/name/flights_v"


# --- Vertex / Edge models ---


def test_vertex_requires_object_key_and_type() -> None:
    v = Vertex(object_key="k", object_type="table")
    assert v.object_key == "k"
    assert v.build is True
    assert v.extra == {}
    with pytest.raises(ValidationError):
        Vertex()  # type: ignore[call-arg]


def test_vertex_extra_ignored() -> None:
    v = Vertex(object_key="k", object_type="table", unknown_field="x")  # type: ignore[call-arg]
    assert v.extra == {}


def test_vertex_object_signature_default_empty() -> None:
    v = Vertex(object_key="k", object_type="table")
    assert v.object_signature == ""


def test_vertex_object_signature_serializes() -> None:
    v = Vertex(object_key="k", object_type="function", object_signature="a1b2c3d4")
    assert v.object_signature == "a1b2c3d4"
    # Field must be present in model_dump so graph_store serializes it.
    assert v.model_dump(mode="json")["object_signature"] == "a1b2c3d4"


def test_add_vertex_distinguishes_overloads_by_signature() -> None:
    """Two functions with the same name but different object_key (one with
    signature suffix, one without) must coexist in the graph instead of
    overwriting each other. Phase 4 fixes the silent-overwrite bug."""
    g = DependencyGraph()
    key_int = "pg_database/db/schema/app/type/function/name/sp_x/signature/a1b2c3d4"
    key_text = "pg_database/db/schema/app/type/function/name/sp_x/signature/e5f6a7b8"
    g.add_vertex(Vertex(object_key=key_int, object_type="function", object_name="sp_x", object_signature="a1b2c3d4"))
    g.add_vertex(Vertex(object_key=key_text, object_type="function", object_name="sp_x", object_signature="e5f6a7b8"))
    assert len(g.vertices) == 2
    assert {v.object_signature for v in g.vertices.values()} == {"a1b2c3d4", "e5f6a7b8"}


def test_edge_dedup_key() -> None:
    e = Edge(
        source_object_key=FLIGHTS,
        destination_object_key=AIRCRAFTS,
        relation=Relation.REFERENCES_BY,
        action="references",
    )
    assert e.dedup_key() == (FLIGHTS, AIRCRAFTS, "REFERENCES_BY", "references")


def test_relation_serializes_as_string() -> None:
    assert Relation.PROVIDE_DATA_TO.value == "PROVIDE_DATA_TO"
    # str-Enum keeps .value as a plain string for JSON dumps.
    assert isinstance(Relation.CHANGE_DATA_IN.value, str)


# --- DependencyGraph mutation ---


def test_add_vertex_replaces_by_key() -> None:
    g = DependencyGraph()
    g.add_vertex(_vertex(AIRCRAFTS))
    g.add_vertex(_vertex(AIRCRAFTS, object_type="view"))  # overwrite
    assert len(g.vertices) == 1
    assert g.get_vertex(AIRCRAFTS).object_type == "view"


def test_add_edge_dedup() -> None:
    g = DependencyGraph()
    g.add_vertex(_vertex(AIRCRAFTS))
    g.add_vertex(_vertex(FLIGHTS))
    e = Edge(source_object_key=FLIGHTS, destination_object_key=AIRCRAFTS, relation=Relation.REFERENCES_BY)
    assert g.add_edge(e) is True
    assert g.add_edge(e) is False  # duplicate ignored
    assert len(g.edges) == 1


def test_add_edge_with_different_action_is_separate() -> None:
    g = DependencyGraph()
    g.add_vertex(_vertex(AIRCRAFTS))
    g.add_vertex(_vertex(FLIGHTS))
    g.add_edge(Edge(source_object_key=FLIGHTS, destination_object_key=AIRCRAFTS, relation=Relation.PROVIDE_DATA_TO, action="select"))
    g.add_edge(Edge(source_object_key=FLIGHTS, destination_object_key=AIRCRAFTS, relation=Relation.PROVIDE_DATA_TO, action="join"))
    # Same endpoints+relation, different action => two distinct edges.
    assert len(g.edges) == 2


# --- lookups ---


def _build_sample_graph() -> DependencyGraph:
    g = DependencyGraph()
    for k in (AIRCRAFTS, FLIGHTS, TICKETS, SEQ, FLIGHTS_V):
        g.add_vertex(_vertex(k))
    g.add_edge(Edge(source_object_key=FLIGHTS, destination_object_key=AIRCRAFTS, relation=Relation.REFERENCES_BY, action="references"))
    g.add_edge(Edge(source_object_key=TICKETS, destination_object_key=FLIGHTS, relation=Relation.REFERENCES_BY, action="references"))
    g.add_edge(Edge(source_object_key=TICKETS, destination_object_key=SEQ, relation=Relation.SEQUENCE_NEXTVAL_IN, action="nextval"))
    g.add_edge(Edge(source_object_key=FLIGHTS_V, destination_object_key=FLIGHTS, relation=Relation.PROVIDE_DATA_TO, action="select"))
    return g


def test_get_dependencies() -> None:
    g = _build_sample_graph()
    deps = g.get_dependencies(TICKETS)
    # Sorted by object_key (sequence < table lexicographically).
    dests = sorted(e.destination_object_key for e in deps)
    assert dests == [SEQ, FLIGHTS]


def test_get_dependents() -> None:
    g = _build_sample_graph()
    dependents = g.get_dependents(FLIGHTS)
    # Sorted by object_key (table/tickets < view/flights_v lexicographically).
    sources = sorted(e.source_object_key for e in dependents)
    assert sources == [TICKETS, FLIGHTS_V]


def test_dangling_edges() -> None:
    g = DependencyGraph()
    g.add_vertex(_vertex(FLIGHTS))
    # Destination missing => dangling.
    g.add_edge(Edge(source_object_key=FLIGHTS, destination_object_key=AIRCRAFTS, relation=Relation.REFERENCES_BY))
    assert len(g.dangling_edges()) == 1


def test_no_dangling_when_complete() -> None:
    g = _build_sample_graph()
    assert g.dangling_edges() == []


# --- transformations ---


def test_filter_build_true_keeps_only_build_vertices() -> None:
    g = DependencyGraph()
    g.add_vertex(_vertex(AIRCRAFTS))
    g.add_vertex(_vertex(FLIGHTS, build=False))  # disabled
    g.add_edge(Edge(source_object_key=FLIGHTS, destination_object_key=AIRCRAFTS, relation=Relation.REFERENCES_BY))
    filtered = g.filter_build_true()

    assert AIRCRAFTS in filtered.vertices
    assert FLIGHTS not in filtered.vertices
    # Edge touching a build=False vertex is dropped from the filtered graph.
    assert filtered.edges == []


def test_filter_build_true_preserves_edges_between_kept() -> None:
    g = _build_sample_graph()
    # Mark FLIGHTS_V as build=False; edges touching it should disappear.
    v = g.vertices[FLIGHTS_V]
    g.vertices[FLIGHTS_V] = v.model_copy(update={"build": False})

    filtered = g.filter_build_true()
    assert FLIGHTS_V not in filtered.vertices
    # The FLIGHTS_V -> FLIGHTS edge is gone, but FLIGHTS -> AIRCRAFTS remains.
    edges_sources = sorted((e.source_object_key, e.destination_object_key) for e in filtered.edges)
    assert (FLIGHTS_V, FLIGHTS) not in edges_sources
    assert (FLIGHTS, AIRCRAFTS) in edges_sources


# --- introspection ---


def test_summary() -> None:
    g = _build_sample_graph()
    assert g.summary() == {"vertices": 5, "edges": 4}


def test_len_is_vertex_count() -> None:
    g = _build_sample_graph()
    assert len(g) == 5


# --- CycleError ---


def test_cycle_error_carries_unresolved() -> None:
    err = CycleError(unresolved=["a", "b", "c"])
    assert err.unresolved == ["a", "b", "c"]
    assert "цикл" in str(err)

"""Tests for db_project_manager.infrastructure.graph.topological_sort."""

from __future__ import annotations

import pytest

from db_project_manager.domain.graph import (
    CycleError,
    DependencyGraph,
    Edge,
    Relation,
    Vertex,
)
from db_project_manager.infrastructure.graph.topological_sort import (
    get_type_priority,
    sort_by_type_and_topology,
    topological_sort,
    TYPE_PRIORITIES,
)


def _v(key: str, object_type: str = "table") -> Vertex:
    return Vertex(
        object_key=key,
        object_catalog="db",
        object_schema="s",
        object_type=object_type,
        object_name=key,
    )


def _edge(src: str, dst: str, relation: Relation = Relation.REFERENCES_BY) -> Edge:
    return Edge(source_object_key=src, destination_object_key=dst, relation=relation)


# --- linear / diamond graphs ---


def test_linear_graph_order() -> None:
    """C depends on B depends on A => deploy order A, B, C."""
    g = DependencyGraph()
    for k in ("A", "B", "C"):
        g.add_vertex(_v(k))
    g.add_edge(_edge("B", "A"))
    g.add_edge(_edge("C", "B"))
    order = [v.object_key for v in topological_sort(g)]
    assert order == ["A", "B", "C"]


def test_diamond_order() -> None:
    """D depends on B and C, both depend on A. D must come after B and C."""
    g = DependencyGraph()
    for k in ("A", "B", "C", "D"):
        g.add_vertex(_v(k))
    g.add_edge(_edge("B", "A"))
    g.add_edge(_edge("C", "A"))
    g.add_edge(_edge("D", "B"))
    g.add_edge(_edge("D", "C"))
    order = [v.object_key for v in topological_sort(g)]
    assert order[0] == "A"
    assert order[-1] == "D"
    assert set(order[1:3]) == {"B", "C"}


def test_disconnected_vertices_all_present() -> None:
    g = DependencyGraph()
    for k in ("A", "B", "C"):
        g.add_vertex(_v(k))
    order = [v.object_key for v in topological_sort(g)]
    assert sorted(order) == ["A", "B", "C"]


# --- cycle detection ---


def test_cycle_raises() -> None:
    """A -> B -> A is a cycle; topological_sort must raise CycleError."""
    g = DependencyGraph()
    g.add_vertex(_v("A"))
    g.add_vertex(_v("B"))
    g.add_edge(_edge("A", "B"))
    g.add_edge(_edge("B", "A"))
    with pytest.raises(CycleError) as exc:
        topological_sort(g)
    assert set(exc.value.unresolved) == {"A", "B"}


def test_self_loop_raises() -> None:
    g = DependencyGraph()
    g.add_vertex(_v("A"))
    g.add_edge(_edge("A", "A"))
    with pytest.raises(CycleError):
        topological_sort(g)


def test_downstream_of_cycle_reported_unresolved() -> None:
    """Cycle (A<->B) blocks C which depends on B. C is reported unresolved too."""
    g = DependencyGraph()
    for k in ("A", "B", "C"):
        g.add_vertex(_v(k))
    g.add_edge(_edge("A", "B"))
    g.add_edge(_edge("B", "A"))
    g.add_edge(_edge("C", "B"))
    with pytest.raises(CycleError) as exc:
        topological_sort(g)
    assert set(exc.value.unresolved) == {"A", "B", "C"}


# --- determinism ---


def test_sort_is_deterministic() -> None:
    """Two runs on the same graph produce identical order."""
    g = DependencyGraph()
    for k in ("aircrafts", "flights", "tickets", "airports"):
        g.add_vertex(_v(k))
    g.add_edge(_edge("flights", "aircrafts"))
    g.add_edge(_edge("flights", "airports"))
    g.add_edge(_edge("tickets", "flights"))
    first = [v.object_key for v in topological_sort(g)]
    second = [v.object_key for v in topological_sort(g)]
    assert first == second


def test_disconnected_sorted_by_key() -> None:
    """Disconnected vertices come out in sorted-by-key order (deterministic)."""
    g = DependencyGraph()
    for k in ("zebra", "alpha", "mike"):
        g.add_vertex(_v(k))
    order = [v.object_key for v in topological_sort(g)]
    assert order == ["alpha", "mike", "zebra"]


# --- type-priority sort ---


def test_type_priority_order() -> None:
    """Schema before sequence before table before view, regardless of edges."""
    g = DependencyGraph()
    g.add_vertex(_v("schema_app", "schema"))
    g.add_vertex(_v("seq", "sequence"))
    g.add_vertex(_v("tbl", "table"))
    g.add_vertex(_v("vw", "view"))
    # No edges — pure type sort.
    order = [v.object_type for v in sort_by_type_and_topology(g)]
    assert order == ["schema", "sequence", "table", "view"]


def test_type_priority_constants() -> None:
    assert TYPE_PRIORITIES["schema"] < TYPE_PRIORITIES["sequence"]
    assert TYPE_PRIORITIES["table"] < TYPE_PRIORITIES["view"]
    assert TYPE_PRIORITIES["view"] < TYPE_PRIORITIES["function"]
    assert get_type_priority("unknown_thing") == 100


def test_type_priority_with_dependency_constraint() -> None:
    """Table (priority 2) must still come after schema (priority 0) even if
    the schema had a higher type — topological constraint wins over type
    only within the same type, but here there's no edge so type decides.
    """
    g = DependencyGraph()
    g.add_vertex(_v("vw", "view"))
    g.add_vertex(_v("tbl", "table"))
    g.add_vertex(_v("sch", "schema"))
    order = [v.object_type for v in sort_by_type_and_topology(g)]
    assert order == ["schema", "table", "view"]


def test_topology_breaks_tie_within_type() -> None:
    """Two tables where B depends on A: A before B despite same type priority."""
    g = DependencyGraph()
    g.add_vertex(_v("B", "table"))
    g.add_vertex(_v("A", "table"))
    g.add_edge(_edge("B", "A"))  # B depends on A
    order = [v.object_key for v in sort_by_type_and_topology(g)]
    assert order == ["A", "B"]


# --- integration with the parser fixture shape ---


def test_sample_codebase_order_puts_tables_before_view() -> None:
    """Mirrors the codebase_sample: flights_v (view) depends on flights (table)."""
    g = DependencyGraph()
    g.add_vertex(_v("aircrafts", "table"))
    g.add_vertex(_v("flights", "table"))
    g.add_vertex(_v("flights_v", "view"))
    g.add_edge(_edge("flights", "aircrafts"))
    g.add_edge(_edge("flights_v", "flights", Relation.PROVIDE_DATA_TO))
    types = [v.object_type for v in sort_by_type_and_topology(g)]
    # All tables before the view.
    assert types.index("table") < types.index("view")

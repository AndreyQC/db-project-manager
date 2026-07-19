"""Topological ordering of graph vertices for deploy sequencing.

Ported from POC dflw_topological_sort.py with three fixes (vision §1.4, risks):

  1. Cycle detection is RE-ENABLED. The POC had the check commented out, so a
     cyclic graph silently dropped objects. We raise CycleError with the list
     of unresolved keys (members of cycles + anything downstream).
  2. Deterministic ordering: neighbours are processed in sorted (by object_key)
     order, so two runs on the same graph produce identical output.
  3. Type-priority-aware sort: within topological constraints, objects are
     ordered by deploy priority (schema < sequence < table < view < ...).
     This is the actual deploy order used by DeployValidateService.

Operates on DependencyGraph from domain/graph.py (not on raw dicts like POC).
"""

from __future__ import annotations

from collections import defaultdict, deque

from db_project_manager.domain.graph import CycleError, DependencyGraph, Vertex

#: Deploy priority by object type. Lower number = deployed earlier.
#: Unknown types get a high value so they go after known ones (defensive).
TYPE_PRIORITIES: dict[str, int] = {
    "schema": 0,
    "sequence": 1,
    "external_table": 2,
    "table": 2,
    "index": 3,
    "constraint": 3,
    "trigger": 3,
    "view": 4,
    "materialized_view": 4,
    "function": 5,
    "procedure": 5,
    "policy": 6,
    "grant": 6,
}

#: Priority assigned to object types not listed above.
UNKNOWN_TYPE_PRIORITY = 100


def get_type_priority(object_type: str) -> int:
    """Return the deploy priority of an object type (lower = earlier)."""
    return TYPE_PRIORITIES.get(object_type, UNKNOWN_TYPE_PRIORITY)


def topological_sort(graph: DependencyGraph) -> list[Vertex]:
    """Return vertices in topological order; raise CycleError on cycles.

    Direction convention: edge source -> destination means 'source depends on
    destination' (e.g. flights REFERENCES_BY aircrafts). Destination must be
    deployed before source, so destination precedes source in the result.
    """
    # Build in-degree: a vertex's in-degree = number of dependencies it has
    # (edges where it is the source). Deployed once all dependencies are deployed.
    in_degree: dict[str, int] = defaultdict(int)
    dependents: dict[str, list[str]] = defaultdict(list)  # dependency -> dependents
    for edge in graph.edges:
        in_degree[edge.source_object_key] += 1
        dependents[edge.destination_object_key].append(edge.source_object_key)
    # Ensure every vertex appears in in_degree (even with no deps).
    for key in graph.vertices:
        in_degree.setdefault(key, 0)

    # Seed queue with all zero-in-degree vertices, sorted for determinism.
    queue: deque[str] = deque(sorted(k for k, d in in_degree.items() if d == 0))
    result: list[Vertex] = []

    while queue:
        current = queue.popleft()
        vertex = graph.get_vertex(current)
        if vertex is not None:
            result.append(vertex)
        # Release dependents in sorted order for deterministic output.
        for dependent in sorted(dependents.get(current, [])):
            in_degree[dependent] -= 1
            if in_degree[dependent] == 0:
                queue.append(dependent)

    if len(result) != len(graph.vertices):
        unresolved = [k for k in graph.vertices if k not in {v.object_key for v in result}]
        raise CycleError(unresolved)

    return result


def sort_by_type_and_topology(graph: DependencyGraph) -> list[Vertex]:
    """Return vertices ordered by (type_priority, topological_order).

    This is the deploy order: type priority wins (schema before table before
    view), topological order breaks ties within the same type. Within a type,
    vertices are further sorted by object_key for reproducibility.
    """
    topo = topological_sort(graph)
    # Assign topological index for tie-breaking.
    topo_index = {v.object_key: i for i, v in enumerate(topo)}
    return sorted(
        topo,
        key=lambda v: (get_type_priority(v.object_type), topo_index[v.object_key], v.object_key),
    )

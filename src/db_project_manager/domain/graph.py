"""Domain model of the object dependency graph.

A DependencyGraph is the in-memory representation of a codebase: vertices are
database objects (read from autodoc headers of SQL files), edges are relations
between them (FK, SELECT, INSERT, nextval, ...). The graph is the shared
artifact between graph build / topological sort / deploy / export.

This module has no I/O and no DB dependencies — pure pydantic models and
graph operations, so it can be unit-tested without fixtures or containers.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Relation(str, Enum):
    """Kind of dependency between two objects.

    str-Enum so values serialize to JSON as plain strings.
    """

    #: Foreign key: child table REFERENCES_BY a parent table.
    REFERENCES_BY = "REFERENCES_BY"
    #: Read access: SELECT / JOIN from a view, materialized view or function.
    PROVIDE_DATA_TO = "PROVIDE_DATA_TO"
    #: Write access: INSERT / UPDATE / MERGE / DELETE / TRUNCATE in a routine.
    CHANGE_DATA_IN = "CHANGE_DATA_IN"
    #: nextval(seq) used as a column default or inside a routine.
    SEQUENCE_NEXTVAL_IN = "SEQUENCE_NEXTVAL_IN"
    #: Soft dependency (e.g. CREATE EXTENSION ... Requires, function calls function).
    DEPENDS_ON = "DEPENDS_ON"


class Vertex(BaseModel):
    """A single database object (node of the graph).

    Fields mirror the autodoc header (infrastructure/sql/autodoc.py):
    object_key is the primary identifier, object_type drives deploy ordering.
    """

    model_config = ConfigDict(extra="ignore")

    object_key: str = Field(..., min_length=1, description="Unique key, e.g. pg_database/db/.../name/t")
    object_catalog: str = ""
    object_schema: str | None = None
    object_type: str = Field(..., description="schema|table|view|function|...")
    object_name: str = ""
    #: Canonical signature hash (8 hex chars) for overloaded functions/procedures.
    #: Empty for non-overloaded objects. Populated by the parser from the
    #: autodoc header; included in ``object_key`` when non-empty so overloads
    #: get distinct keys instead of silently overwriting each other in the graph.
    object_signature: str = Field("", description="Canonical signature hash for overloaded functions/procedures")
    #: Raw comma-joined argument type list for overloaded functions/procedures
    #: (e.g. "int4", "text,varchar"), as produced by the adapter from
    #: ``pg_type.typname``. Empty for non-routine / no-arg objects. Populated by
    #: the parser from the autodoc header; used by overload resolution (Phase 8)
    #: to match a call site to a specific overload. Carries DATA for type
    #: inference, not identity (identity remains object_key — LESSONS §26).
    argument_types: str = Field("", description="Raw argument types for overloaded functions/procedures")
    object_source_file: str = Field("", description="Path to the source SQL file")
    build: bool = Field(True, description="Whether this object is deployed (project.build in autodoc)")
    #: Phase 10 (CDF-10): marker that this object is managed by db-pm and must
    #: not be hand-edited. Set by reverse-engineer for objects in the service
    #: schema (``__deploy``). Lives in the autodoc ``project`` section next to
    #: ``build``; omitted (False) for ordinary objects.
    immutable: bool = Field(False, description="Managed by db-pm; do not hand-edit (project.immutable in autodoc)")
    extra: dict[str, Any] = Field(default_factory=dict, description="Raw autodoc fields not covered above")


class Edge(BaseModel):
    """A directed dependency from one object to another.

    direction: source_object_key DEPENDS-ON destination_object_key.
    (e.g. flights --REFERENCES_BY--> aircrafts)
    """

    model_config = ConfigDict(extra="ignore")

    source_object_key: str
    destination_object_key: str
    relation: Relation
    action: str = Field("", description="select|insert|update|delete|truncate|merge|nextval|references|join...")

    def dedup_key(self) -> tuple[str, str, str, str]:
        """Hashable identity of the edge for de-duplication.

        Replaces the POC's set(tuple(sorted(d.items()))) which broke on mixed
        value types (None vs str). Keep this in sync with any new fields that
        identify an edge uniquely.
        """
        return (self.source_object_key, self.destination_object_key, self.relation.value, self.action)


class CycleError(Exception):
    """Raised when the graph has cycles and topological sort is impossible.

    Carries the set of object keys that could not be ordered (cycle members
    plus anything downstream blocked by the cycle).
    """

    def __init__(self, unresolved: list[str]) -> None:
        self.unresolved = list(unresolved)
        preview = ", ".join(sorted(unresolved)[:10])
        super().__init__(f"Граф содержит циклы (нельзя отсортировать): {preview}{' ...' if len(unresolved) > 10 else ''}")


class DependencyGraph:
    """Mutable in-memory graph with lookup helpers.

    Plain class (not pydantic) on purpose: we expose the dict-of-Vertex view
    and keep edges as a list. Serialization for .dbm_graph/ lives in graph_store.
    """

    def __init__(
        self,
        vertices: dict[str, Vertex] | None = None,
        edges: list[Edge] | None = None,
    ) -> None:
        self.vertices: dict[str, Vertex] = dict(vertices) if vertices else {}
        # Deduplicate edges on add; keep a side index for fast lookup.
        self.edges: list[Edge] = []
        self._edge_keys: set[tuple[str, str, str, str]] = set()
        if edges:
            for edge in edges:
                self.add_edge(edge)

    # --- mutation ---

    def add_vertex(self, vertex: Vertex) -> None:
        """Insert or replace a vertex by its object_key."""
        self.vertices[vertex.object_key] = vertex

    def add_edge(self, edge: Edge) -> bool:
        """Add an edge, ignoring exact duplicates.

        Returns True if the edge was new, False if it was already present.
        """
        key = edge.dedup_key()
        if key in self._edge_keys:
            return False
        self._edge_keys.add(key)
        self.edges.append(edge)
        return True

    # --- lookups ---

    def get_vertex(self, object_key: str) -> Vertex | None:
        return self.vertices.get(object_key)

    def get_dependencies(self, object_key: str) -> list[Edge]:
        """Edges where object_key is the source: what this object depends on."""
        return [e for e in self.edges if e.source_object_key == object_key]

    def get_dependents(self, object_key: str) -> list[Edge]:
        """Edges where object_key is the destination: what depends on this object."""
        return [e for e in self.edges if e.destination_object_key == object_key]

    def dangling_edges(self) -> list[Edge]:
        """Edges whose endpoint vertex is missing (orphans / unresolved references)."""
        return [
            e
            for e in self.edges
            if e.source_object_key not in self.vertices or e.destination_object_key not in self.vertices
        ]

    # --- transformations ---

    def filter_build_true(self) -> DependencyGraph:
        """Return a graph keeping only build=True vertices and edges between them.

        Phase 2 decision (vision Q8): the full graph keeps build=False vertices,
        but deploy validate operates on this filtered view.
        """
        keep = {k for k, v in self.vertices.items() if v.build}
        out = DependencyGraph(vertices={k: self.vertices[k] for k in keep})
        for edge in self.edges:
            if edge.source_object_key in keep and edge.destination_object_key in keep:
                out.add_edge(edge)
        return out

    # --- introspection ---

    def __len__(self) -> int:
        return len(self.vertices)

    def summary(self) -> dict[str, int]:
        return {"vertices": len(self.vertices), "edges": len(self.edges)}

"""Snapshot builder for the compare feature (Phase 9).

A :class:`~db_project_manager.domain.diff.StateSnapshot` is the flat representation
of one comparison side, derived from a reverse-engineer codebase directory. We
reuse the existing dependency graph parser (:class:`BuildGraphService`) to get the
object inventory (vertices with their ``object_key``/``object_source_file``), then
read each object's SQL body, strip the autodoc header, normalize it (sqlglot), and
hash it. The hash is what the comparator compares for "changed vs unchanged".

Only structural object types participate in the diff (draft §2, decision 2):
extensions and database settings are excluded because they tend to differ between
environments and would noise up the report.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from db_project_manager.application.graph_service import BuildGraphService
from db_project_manager.domain.diff import EdgeSnapshot, ObjectSnapshot, SnapshotSourceKind, StateSnapshot
from db_project_manager.domain.graph import DependencyGraph
from db_project_manager.infrastructure.diff.columns import extract_columns
from db_project_manager.infrastructure.diff.normalize_sql import normalize_sql, sql_hash
from db_project_manager.infrastructure.sql.autodoc import MARKER_CLOSE

#: Object types that participate in the structural diff.
#: Extensions and database settings are excluded (draft §2, decision 2): they
#: vary between environments and would produce noisy diffs.
#:
#: ``schema`` is included so that ``deploy apply`` on an empty target DB writes
#: ``CREATE SCHEMA`` artifacts before the tables that depend on them. Without
#: this, a schema-less snapshot would silently drop every schema vertex and the
#: resulting DeltaPlan would try to ``CREATE TABLE "schema"."t"`` in a schema
#: that does not yet exist (bugfix after cis_zup feedback 2026-09-02).
DIFFED_TYPES = frozenset({
    "schema",
    "table", "view", "materialized_view",
    "function", "procedure", "sequence",
})

#: Edge relations that participate in the edge diff (Phase 14). All relations are
#: included; the comparator de-duplicates via Edge.dedup_key(). Restrict here only
#: if a relation turns out to be too noisy between environments.
DIFFED_EDGE_RELATIONS: frozenset[str] | None = None


def build_snapshot_from_dir(
    codebase_dir: str | Path,
    *,
    source_kind: SnapshotSourceKind,
    source_ref: str,
    db_type: str,
    row_counts: dict[tuple[str, str], int | float | None] | None = None,
    graph_service: BuildGraphService | None = None,
) -> StateSnapshot:
    """Build a :class:`StateSnapshot` from a reverse-engineer codebase dir.

    Args:
        codebase_dir: root of the reverse-engineer tree (contains ``<schema>/`` dirs).
        source_kind: DB or DIR — recorded in the snapshot for the report.
        source_ref: connection name (DB) or directory path (DIR).
        db_type: ``postgres`` or ``greenplum`` — must match the other side to compare.
        row_counts: optional ``{(schema, table): estimated_rows}`` from
            :meth:`DatabaseAdapter.get_table_row_counts`; attached to tables only.
        graph_service: injectable for tests (defaults to a fresh ``BuildGraphService``).

    The graph is read-only here; no ``.dbm_graph/`` is written.
    """
    root = Path(codebase_dir)
    service = graph_service or BuildGraphService()
    graph: DependencyGraph = service.build(root)

    objects: dict[str, ObjectSnapshot] = {}
    for vertex in graph.vertices.values():
        if vertex.object_type not in DIFFED_TYPES:
            continue
        body = _read_sql_body(root, vertex.object_source_file)
        normalized = normalize_sql(body)
        objects[vertex.object_key] = ObjectSnapshot(
            object_key=vertex.object_key,
            object_schema=vertex.object_schema,
            object_name=vertex.object_name,
            object_type=vertex.object_type,
            object_signature=vertex.object_signature,
            sql_normalized=normalized,
            sql_hash=sql_hash(normalized),
            estimated_rows=_lookup_row_count(row_counts, vertex.object_schema, vertex.object_name, vertex.object_type),
            # Phase 12 (ALT-1b): table columns come from the SQL body itself —
            # both sides (codebase files and RE-generated DDL) use the same
            # extractor; None = unavailable → fail-safe downstream.
            columns=extract_columns(body) if vertex.object_type == "table" else None,
        )

    return StateSnapshot(
        source_kind=source_kind,
        source_ref=source_ref,
        db_type=db_type,
        generated_at=datetime.now(timezone.utc).isoformat(),
        objects=objects,
        edges=_collect_edges(graph),
    )


def _read_sql_body(codebase_root: Path, source_file: str) -> str:
    """Read an object's SQL body, stripping the autodoc comment header.

    The autodoc block is a ``/* ... [[autodoc-yaml]>] ... */`` comment at the top
    of the file. We take everything after the closing ``*/`` of that block. If no
    autodoc is present, the whole file content is returned.
    """
    if not source_file:
        return ""
    path = codebase_root / source_file
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8-sig")
    if MARKER_CLOSE not in text:
        return text.strip()
    after_marker = text.split(MARKER_CLOSE, 1)[1]
    if "*/" in after_marker:
        after_marker = after_marker.split("*/", 1)[1]
    return after_marker.strip()


def _lookup_row_count(
    row_counts: dict[tuple[str, str], int | float | None] | None,
    schema: str | None,
    name: str,
    object_type: str,
) -> int | None:
    """Attach estimated_rows to tables; None for all other object types."""
    if object_type != "table" or row_counts is None or schema is None:
        return None
    value = row_counts.get((schema, name))
    if value is None:
        return None
    return int(value)


def _collect_edges(graph: DependencyGraph) -> list[EdgeSnapshot]:
    """Collect graph edges as :class:`EdgeSnapshot`, de-duplicated by dedup_key.

    Phase 14 edge-diff: the comparator compares edge identity (the same
    :meth:`Edge.dedup_key` the graph uses for de-duplication, LESSONS §16). Edges
    whose endpoints are not in :data:`DIFFED_TYPES` are dropped — an edge to/from an
    extension or a database setting would always look "removed" since those types
    are excluded from the object diff.

    The result is sorted by dedup_key so the output (and the diff) is deterministic.
    """
    object_keys = {v.object_key for v in graph.vertices.values() if v.object_type in DIFFED_TYPES}
    seen: dict[tuple[str, str, str, str], EdgeSnapshot] = {}
    for edge in graph.edges:
        if edge.source_object_key not in object_keys or edge.destination_object_key not in object_keys:
            continue
        snap = EdgeSnapshot(
            source_object_key=edge.source_object_key,
            destination_object_key=edge.destination_object_key,
            relation=edge.relation.value,
            action=edge.action,
        )
        seen.setdefault(snap.dedup_key(), snap)
    return [seen[k] for k in sorted(seen)]

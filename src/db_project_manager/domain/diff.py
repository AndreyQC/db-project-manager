"""Domain models for the DB-vs-filesystem comparison feature (Phase 9).

A comparison runs between two *sides*: each side is either a live database
connection or an existing reverse-engineer codebase directory on disk. Both
sides are normalized into a :class:`StateSnapshot` — a flat dict of objects
keyed by ``object_key`` (the same identity used by the dependency graph). The
comparison then produces a :class:`DiffReport` with per-object statuses.

This module is pure pydantic — no I/O, no DB, no sqlglot. Serialization for
``source.json``/``target.json``/``diff_report.json`` uses the standard pydantic
JSON machinery.

See ``_docs_/_tasks_/2026-07-28/20260728_001_compare_db_vs_fs_draft.md`` for
the design decisions (granularity, db_type compatibility, report format).
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict

from db_project_manager.domain.delta import ColumnDiff, ColumnSnapshot


class SnapshotSourceKind(str, Enum):
    """What a comparison side is backed by.

    str-Enum so values serialize to JSON as plain strings.
    """

    DB = "db"      # a live database connection
    DIR = "dir"    # a reverse-engineer codebase directory on disk


class DiffStatus(str, Enum):
    """Per-object comparison result.

    Direction: ``added``/``removed`` are relative to the *source* side.
    ``added`` = present in source, missing in target;
    ``removed`` = missing in source, present in target.
    """

    ADDED = "added"
    REMOVED = "removed"
    CHANGED = "changed"
    UNCHANGED = "unchanged"


#: ``DiffReport.summary`` key for objects excluded from the whole diff because
#: their autodoc carries ``project.build: false`` (Phase 15.7). Such objects are
#: not managed by the deploy tooling; the count keeps their absence explainable.
IGNORED_BUILD_FALSE_KEY = "ignored_build_false"


class CodebaseManifest(BaseModel):
    """Properties of a whole database, written next to a reverse-engineer tree.

    ``dbpm.manifest.json`` in the codebase root lets downstream tooling (the
    compare feature) know the source DB type without re-reading the connection.
    No secrets — only type, name and timestamps.

    Phase 10 added ``source_version`` (calver ``YYYY.MM.DD.NN``) — the version
    of the codebase, MR-controlled, recorded into ``__deploy.schema_version`` on
    deploy. Empty default keeps v1 backwards-compatible on read; writers and v2
    readers enforce presence + calver validity (see ``codebase_manifest`` module).
    """

    db_type: str              # "postgres" | "greenplum" (from ConnectionConfig.type)
    database: str             # the database name (object_catalog)
    generated_at: str         # UTC ISO timestamp of the reverse-engineer run
    tool_version: str = ""    # db-pm version (informational, compatibility aid)
    format_version: int = 1   # manifest schema version
    source_version: str = ""  # calver YYYY.MM.DD.NN; required at format_version>=2


class ObjectSnapshot(BaseModel):
    """One object within a :class:`StateSnapshot` (a graph vertex + its SQL)."""

    model_config = ConfigDict(extra="ignore")

    object_key: str
    object_schema: str | None = None
    object_name: str = ""
    object_type: str
    object_signature: str = ""
    sql_normalized: str       # sqlglot-normalized SQL body (audit/debug)
    sql_hash: str             # 8 hex SHA-256 of sql_normalized
    estimated_rows: int | None = None  # tables only (reltuples); None otherwise
    # Phase 15.7: the autodoc ``project.build`` flag. Objects with build=false
    # are excluded from the diff on BOTH sides (they are not managed by the
    # deploy tooling) — see CompareService._exclude_build_false.
    build: bool = True
    # Phase 12 (ALT-1b): columns extracted from the SQL body at snapshot-build time.
    # None = not extracted (non-table object, unparseable DDL) → structural diff
    # unavailable → fail-safe downstream; [] = extracted, the table has no columns.
    columns: list[ColumnSnapshot] | None = None


class StateSnapshot(BaseModel):
    """All objects of one comparison side, ready to be compared.

    ``objects`` is a dict keyed by ``object_key`` so set operations on keys give
    added/removed directly, and ``sql_hash`` comparison gives changed/unchanged.

    ``edges`` (Phase 14, edge-diff) captures the dependency-graph edges of this side,
    so they can be compared too. Additive field with a default; old snapshots without
    ``edges`` parse fine (empty list).
    """

    model_config = ConfigDict(extra="ignore")

    source_kind: SnapshotSourceKind
    source_ref: str           # codebase path or connection name
    db_type: str              # postgres | greenplum (from manifest or connection)
    generated_at: str         # UTC ISO timestamp of the snapshot
    objects: dict[str, ObjectSnapshot]
    edges: list[EdgeSnapshot] = []  # Phase 14: graph edges of this side


class DiffEntry(BaseModel):
    """A single object's comparison outcome.

    ``source_snapshot``/``target_snapshot`` are populated depending on status:
    added → source only; removed → target only; changed/unchanged → both.
    """

    model_config = ConfigDict(extra="ignore")

    object_key: str
    status: DiffStatus
    source_snapshot: ObjectSnapshot | None = None
    target_snapshot: ObjectSnapshot | None = None
    # Phase 12 (CD-ALT-1): column-level detail for CHANGED tables — filled by the
    # comparator when both sides carry extracted columns. Additive defaults keep old
    # reports parsing.
    column_diffs: list[ColumnDiff] = []
    # True = at least one side has columns=None → structural diff impossible for this
    # entry; the classifier must treat any change as unrepresented (fail-safe, ALT-2).
    columns_unavailable: bool = False


class EdgeSnapshot(BaseModel):
    """A dependency-graph edge on one comparison side (Phase 14, edge-diff).

    Mirrors :meth:`db_project_manager.domain.graph.Edge.dedup_key` so two edges are
    the same iff these four fields match (the same identity the graph uses for
    de-duplication — LESSONS §16).
    """

    model_config = ConfigDict(extra="ignore")

    source_object_key: str
    destination_object_key: str
    relation: str             # Relation.value (e.g. "depends_on", "provides_data_to")
    action: str               # "select" / "references" / "nextval" / "call" / ...

    def dedup_key(self) -> tuple[str, str, str, str]:
        return (self.source_object_key, self.destination_object_key, self.relation, self.action)


class EdgeDiffEntry(BaseModel):
    """One edge added to or removed from the graph between two states.

    Edges don't "change" — they appear (added) or disappear (removed). ``source_edge``
    is set for ``added`` (the edge exists in source, not target); ``target_edge`` for
    ``removed``.
    """

    model_config = ConfigDict(extra="ignore")

    status: DiffStatus        # ADDED or REMOVED
    source_edge: EdgeSnapshot | None = None
    target_edge: EdgeSnapshot | None = None


class DiffReport(BaseModel):
    """The full comparison result: two snapshots + per-object entries + counts.

    ``edge_summary`` / ``edge_entries`` (Phase 14, edge-diff) are additive fields
    with defaults; old reports without them parse fine.
    """

    model_config = ConfigDict(extra="ignore")

    source: StateSnapshot
    target: StateSnapshot
    generated_at: str         # UTC ISO timestamp of the comparison run
    summary: dict[str, int]   # {"added": N, "removed": N, "changed": N, "unchanged": N}
    entries: list[DiffEntry]
    edge_summary: dict[str, int] = {}   # {"added": N, "removed": N}
    edge_entries: list[EdgeDiffEntry] = []

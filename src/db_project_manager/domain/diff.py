"""Domain models for the DB-vs-filesystem comparison feature (Phase 9).

A comparison runs between two *sides*: each side is either a live database
connection or an existing reverse-engineer codebase directory on disk. Both
sides are normalized into a :class:`StateSnapshot` — a flat dict of objects
keyed by ``object_key`` (the same identity used by the dependency graph). The
comparison then produces a :class:`DiffReport` with per-object statuses.

This module is pure pydantic — no I/O, no DB, no sqlglot. Serialization for
``source.json``/``target.json``/``diff_report.json`` uses the standard pydantic
JSON machinery.

See ``-=docs=-/-=tasks=-/2026-07-28/20260728_001_compare_db_vs_fs_draft.md`` for
the design decisions (granularity, db_type compatibility, report format).
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict


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


class CodebaseManifest(BaseModel):
    """Properties of a whole database, written next to a reverse-engineer tree.

    ``dbpm.manifest.json`` in the codebase root lets downstream tooling (the
    compare feature) know the source DB type without re-reading the connection.
    No secrets — only type, name and timestamps.
    """

    db_type: str              # "postgres" | "greenplum" (from ConnectionConfig.type)
    database: str             # the database name (object_catalog)
    generated_at: str         # UTC ISO timestamp of the reverse-engineer run
    tool_version: str = ""    # db-pm version (informational, compatibility aid)
    format_version: int = 1   # manifest schema version


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


class StateSnapshot(BaseModel):
    """All objects of one comparison side, ready to be compared.

    ``objects`` is a dict keyed by ``object_key`` so set operations on keys give
    added/removed directly, and ``sql_hash`` comparison gives changed/unchanged.
    """

    model_config = ConfigDict(extra="ignore")

    source_kind: SnapshotSourceKind
    source_ref: str           # codebase path or connection name
    db_type: str              # postgres | greenplum (from manifest or connection)
    generated_at: str         # UTC ISO timestamp of the snapshot
    objects: dict[str, ObjectSnapshot]


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


class DiffReport(BaseModel):
    """The full comparison result: two snapshots + per-object entries + counts."""

    model_config = ConfigDict(extra="ignore")

    source: StateSnapshot
    target: StateSnapshot
    generated_at: str         # UTC ISO timestamp of the comparison run
    summary: dict[str, int]   # {"added": N, "removed": N, "changed": N, "unchanged": N}
    entries: list[DiffEntry]

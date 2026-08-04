"""Grouping helpers for diff entries (Phase 14).

Pure functions shared by the markdown report generator
(:mod:`infrastructure.diff.markdown_report`) and the Delta Viewer GUI. Kept here so the
GUI does not have to depend on the markdown module — both depend on this small,
I/O-free helper.

Grouping order:
    status -> object_type -> object_schema (None last within a type) -> [entries]

``object_type`` follows the canonical ``DIFFED_TYPES`` order; types outside that set
(should not happen, but defensively) sort after, alphabetically.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Protocol

from db_project_manager.domain.diff import DiffEntry, DiffStatus
from db_project_manager.infrastructure.diff.snapshot import DIFFED_TYPES

#: Stable type order: DIFFED_TYPES first (in their canonical order), then anything else.
_TYPE_PRIORITY: dict[str, int] = {t: i for i, t in enumerate(DIFFED_TYPES)}


def type_priority(object_type: str) -> int:
    """Sort key for object types — DIFFED_TYPES first, then alphabetical by priority."""
    return _TYPE_PRIORITY.get(object_type, len(_TYPE_PRIORITY) + 1)


class _SnapLike(Protocol):
    """Structural type for ObjectSnapshot — only the fields grouping needs."""

    object_schema: str | None
    object_name: str
    object_type: str
    object_signature: str


def _entry_snapshot(entry: DiffEntry) -> _SnapLike | None:
    """Return the 'present' snapshot of an entry (source for added; target for removed)."""
    if entry.source_snapshot is not None:
        return entry.source_snapshot
    return entry.target_snapshot


def group_entries_by_status(
    entries: list[DiffEntry],
    status: DiffStatus,
) -> dict[str, dict[str | None, list[DiffEntry]]]:
    """Group entries of one status by ``object_type`` then ``object_schema``.

    Returns ``{object_type: {schema: [entries, ...]}}``. Schemas are keyed by their
    string value, with ``None`` (schema-less objects) represented as ``None``.

    Types are NOT sorted here (caller sorts via :func:`type_priority`); schemas keep
    insertion order (callers may sort if needed).
    """
    by_type: dict[str, dict[str | None, list[DiffEntry]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for entry in entries:
        if entry.status is not status:
            continue
        snap = _entry_snapshot(entry)
        if snap is None:
            # Defensive: a malformed entry without any snapshot — skip rather than crash.
            continue
        by_type[snap.object_type][snap.object_schema].append(entry)
    return by_type


def schema_label(schema: str | None) -> str:
    """Human-readable label for a schema bucket (None -> a dash placeholder)."""
    return schema if schema else "—"

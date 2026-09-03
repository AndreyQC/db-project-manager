"""Comparator for the diff feature (Phase 9).

Given two :class:`~db_project_manager.domain.diff.StateSnapshot` instances (already
validated as db_type-compatible by :class:`CompareService`), produce a
:class:`~db_project_manager.domain.diff.DiffReport`:

- ``added``     — object_key present in *source*, missing in *target*
- ``removed``   — object_key missing in *source*, present in *target*
- ``changed``   — present in both, ``sql_hash`` differs
- ``unchanged`` — present in both, ``sql_hash`` equal

The comparison is purely on the snapshots' data (dicts keyed by ``object_key``);
no file/DB access. Identity matching relies on the stable ``object_key`` format
(including the ``/signature/<hash>`` suffix for overloaded functions, so two
overloads never collapse into one entry — lesson §26).
"""

from __future__ import annotations

from datetime import datetime, timezone

from db_project_manager.domain.diff import (
    DiffEntry,
    DiffReport,
    DiffStatus,
    EdgeDiffEntry,
    StateSnapshot,
)
from db_project_manager.infrastructure.diff.columns import diff_columns


def identity_key(object_key: str) -> str:
    """Object identity with the environment-specific catalog stripped.

    ``object_key`` starts with ``pg_database/<catalog>/`` — the database NAME
    differs between environments (dev vs prod, or the Phase 12 rehearsal DB
    reproducing the target under another name). Identity for comparison is
    ``schema/<s>/type/<t>/name/<n>[/signature/<h>]``; keys without the prefix
    (synthetic tests) pass through unchanged.
    """
    if object_key.startswith("pg_database/"):
        parts = object_key.split("/", 2)
        if len(parts) == 3:
            return parts[2]
    return object_key


def _edge_identity(edge) -> tuple[str, str, str, str]:
    """Edge dedup_key with catalog-insensitive endpoint keys."""
    return (
        identity_key(edge.source_object_key),
        identity_key(edge.destination_object_key),
        edge.relation,
        edge.action,
    )


def compare(source: StateSnapshot, target: StateSnapshot) -> DiffReport:
    """Compare two snapshots by ``object_key`` identity + ``sql_hash``.

    The caller (:class:`CompareService`) is responsible for verifying
    ``source.db_type == target.db_type`` before calling this; the comparator
    assumes the pair is already compatible. Keys are matched by
    :func:`identity_key` (catalog-insensitive); entries carry the source-side
    key when present, else the target-side one.
    """
    src_keys = {identity_key(k): k for k in source.objects}
    tgt_keys = {identity_key(k): k for k in target.objects}

    added_keys = src_keys.keys() - tgt_keys.keys()
    removed_keys = tgt_keys.keys() - src_keys.keys()
    common_keys = src_keys.keys() & tgt_keys.keys()

    entries: list[DiffEntry] = []

    for key in sorted(added_keys):
        entries.append(DiffEntry(
            object_key=src_keys[key],
            status=DiffStatus.ADDED,
            source_snapshot=source.objects[src_keys[key]],
        ))

    for key in sorted(removed_keys):
        entries.append(DiffEntry(
            object_key=tgt_keys[key],
            status=DiffStatus.REMOVED,
            target_snapshot=target.objects[tgt_keys[key]],
        ))

    changed = 0
    unchanged = 0
    for key in sorted(common_keys):
        src_obj = source.objects[src_keys[key]]
        tgt_obj = target.objects[tgt_keys[key]]
        # Phase 15.5.5 (cis_zup feedback 2026-09-04): sql_hash is a coarse signal
        # that does NOT see Phase 15.5.4's column-level compensation (serial/int
        # canonicalisation, nextval default-equivalence). When the hash differs
        # but the column diffs are empty (after Phase 15.5.4 normalisation),
        # the two sides are structurally equal — declare UNCHANGED so the
        # safety gate / delta-plan don't get false-positive CHANGED.
        column_diffs: list = []
        columns_unavailable = False
        if src_obj.object_type == "table":
            if src_obj.columns is None or tgt_obj.columns is None:
                columns_unavailable = True
            else:
                column_diffs = diff_columns(src_obj.columns, tgt_obj.columns)

        hash_agrees = src_obj.sql_hash == tgt_obj.sql_hash
        columns_match = (
            src_obj.object_type == "table"
            and not columns_unavailable
            and not column_diffs
        )

        if hash_agrees or columns_match:
            unchanged += 1
            entries.append(DiffEntry(
                object_key=src_keys[key],
                status=DiffStatus.UNCHANGED,
                source_snapshot=src_obj,
                target_snapshot=tgt_obj,
                column_diffs=[],
                columns_unavailable=False,
            ))
        else:
            changed += 1
            entries.append(DiffEntry(
                object_key=src_keys[key],
                status=DiffStatus.CHANGED,
                source_snapshot=src_obj,
                target_snapshot=tgt_obj,
                column_diffs=column_diffs,
                columns_unavailable=columns_unavailable,
            ))

    summary = {
        DiffStatus.ADDED.value: len(added_keys),
        DiffStatus.REMOVED.value: len(removed_keys),
        DiffStatus.CHANGED.value: changed,
        DiffStatus.UNCHANGED.value: unchanged,
    }

    # Edge diff (Phase 14): edges appear (added) or disappear (removed) between the
    # two states. Compared by dedup_key (the same identity the graph uses, LESSONS §16)
    # with catalog-insensitive endpoint keys (see identity_key); edges to/from
    # non-DIFFED_TYPES were already dropped by the snapshot builder.
    src_edge_keys = {_edge_identity(e): e for e in source.edges}
    tgt_edge_keys = {_edge_identity(e): e for e in target.edges}
    edge_added_keys = src_edge_keys.keys() - tgt_edge_keys.keys()
    edge_removed_keys = tgt_edge_keys.keys() - src_edge_keys.keys()

    edge_entries: list[EdgeDiffEntry] = []
    for key in sorted(edge_added_keys):
        edge_entries.append(EdgeDiffEntry(status=DiffStatus.ADDED, source_edge=src_edge_keys[key]))
    for key in sorted(edge_removed_keys):
        edge_entries.append(EdgeDiffEntry(status=DiffStatus.REMOVED, target_edge=tgt_edge_keys[key]))

    edge_summary = {
        DiffStatus.ADDED.value: len(edge_added_keys),
        DiffStatus.REMOVED.value: len(edge_removed_keys),
    }

    return DiffReport(
        source=source,
        target=target,
        generated_at=datetime.now(timezone.utc).isoformat(),
        summary=summary,
        entries=entries,
        edge_summary=edge_summary,
        edge_entries=edge_entries,
    )

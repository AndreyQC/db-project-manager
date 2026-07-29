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
    StateSnapshot,
)


def compare(source: StateSnapshot, target: StateSnapshot) -> DiffReport:
    """Compare two snapshots by ``object_key`` set + ``sql_hash``.

    The caller (:class:`CompareService`) is responsible for verifying
    ``source.db_type == target.db_type`` before calling this; the comparator
    assumes the pair is already compatible.
    """
    src_keys = set(source.objects)
    tgt_keys = set(target.objects)

    added_keys = src_keys - tgt_keys
    removed_keys = tgt_keys - src_keys
    common_keys = src_keys & tgt_keys

    entries: list[DiffEntry] = []

    for key in sorted(added_keys):
        entries.append(DiffEntry(
            object_key=key,
            status=DiffStatus.ADDED,
            source_snapshot=source.objects[key],
        ))

    for key in sorted(removed_keys):
        entries.append(DiffEntry(
            object_key=key,
            status=DiffStatus.REMOVED,
            target_snapshot=target.objects[key],
        ))

    changed = 0
    unchanged = 0
    for key in sorted(common_keys):
        src_obj = source.objects[key]
        tgt_obj = target.objects[key]
        if src_obj.sql_hash == tgt_obj.sql_hash:
            unchanged += 1
            entries.append(DiffEntry(
                object_key=key,
                status=DiffStatus.UNCHANGED,
                source_snapshot=src_obj,
                target_snapshot=tgt_obj,
            ))
        else:
            changed += 1
            entries.append(DiffEntry(
                object_key=key,
                status=DiffStatus.CHANGED,
                source_snapshot=src_obj,
                target_snapshot=tgt_obj,
            ))

    summary = {
        DiffStatus.ADDED.value: len(added_keys),
        DiffStatus.REMOVED.value: len(removed_keys),
        DiffStatus.CHANGED.value: changed,
        DiffStatus.UNCHANGED.value: unchanged,
    }

    return DiffReport(
        source=source,
        target=target,
        generated_at=datetime.now(timezone.utc).isoformat(),
        summary=summary,
        entries=entries,
    )

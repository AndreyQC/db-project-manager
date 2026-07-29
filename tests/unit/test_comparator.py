"""Unit tests for the comparator (Phase 9)."""

from __future__ import annotations

from db_project_manager.domain.diff import (
    DiffStatus,
    ObjectSnapshot,
    SnapshotSourceKind,
    StateSnapshot,
)
from db_project_manager.infrastructure.diff.comparator import compare


def _obj(key: str, sql_hash: str = "aaaa0000", **overrides) -> ObjectSnapshot:
    base = {
        "object_key": key,
        "object_schema": "public",
        "object_name": key.split("name/")[-1],
        "object_type": "table",
        "sql_normalized": "CREATE TABLE t (a INT)",
        "sql_hash": sql_hash,
    }
    base.update(overrides)
    return ObjectSnapshot(**base)


def _state(objects: dict[str, ObjectSnapshot], **overrides) -> StateSnapshot:
    base = {
        "source_kind": SnapshotSourceKind.DIR,
        "source_ref": "src",
        "db_type": "postgres",
        "generated_at": "2026-07-29T00:00:00+00:00",
        "objects": objects,
    }
    base.update(overrides)
    return StateSnapshot(**base)


# --- single-status scenarios ---


def test_all_unchanged_when_identical_hashes():
    src = _state({"k1": _obj("k1"), "k2": _obj("k2", "bbbb0000")})
    tgt = _state({"k1": _obj("k1"), "k2": _obj("k2", "bbbb0000")})
    report = compare(src, tgt)
    assert report.summary == {"added": 0, "removed": 0, "changed": 0, "unchanged": 2}


def test_all_added_when_target_empty():
    src = _state({"k1": _obj("k1")})
    tgt = _state({})
    report = compare(src, tgt)
    assert report.summary == {"added": 1, "removed": 0, "changed": 0, "unchanged": 0}
    assert report.entries[0].status == DiffStatus.ADDED
    assert report.entries[0].source_snapshot is not None
    assert report.entries[0].target_snapshot is None


def test_all_removed_when_source_empty():
    src = _state({})
    tgt = _state({"k1": _obj("k1")})
    report = compare(src, tgt)
    assert report.summary == {"added": 0, "removed": 1, "changed": 0, "unchanged": 0}
    assert report.entries[0].status == DiffStatus.REMOVED
    assert report.entries[0].target_snapshot is not None
    assert report.entries[0].source_snapshot is None


def test_changed_when_hash_differs():
    src = _state({"k1": _obj("k1", "aaaa0000")})
    tgt = _state({"k1": _obj("k1", "bbbb0000")})
    report = compare(src, tgt)
    assert report.summary == {"added": 0, "removed": 0, "changed": 1, "unchanged": 0}
    entry = report.entries[0]
    assert entry.status == DiffStatus.CHANGED
    assert entry.source_snapshot is not None
    assert entry.target_snapshot is not None


# --- mixed scenario ---


def test_mixed_scenario_one_of_each_status():
    src = _state({
        "added_only": _obj("added_only"),
        "changed": _obj("changed", "aaaa0000"),
        "unchanged": _obj("unchanged", "cccc0000"),
    })
    tgt = _state({
        "changed": _obj("changed", "bbbb0000"),
        "unchanged": _obj("unchanged", "cccc0000"),
        "removed_only": _obj("removed_only"),
    })
    report = compare(src, tgt)
    assert report.summary == {"added": 1, "removed": 1, "changed": 1, "unchanged": 1}


# --- edge cases ---


def test_both_empty_snapshots():
    report = compare(_state({}), _state({}))
    assert report.summary == {"added": 0, "removed": 0, "changed": 0, "unchanged": 0}
    assert report.entries == []


def test_overloads_distinct_by_signature_key():
    """Two overloads (same name, different object_key) are compared independently."""
    src = _state({
        "pg/db/type/function/name/sp_x/signature/aaa": _obj("pg/db/type/function/name/sp_x/signature/aaa"),
    })
    tgt = _state({
        "pg/db/type/function/name/sp_x/signature/bbb": _obj("pg/db/type/function/name/sp_x/signature/bbb"),
    })
    report = compare(src, tgt)
    # Different keys → one added (in src), one removed (in tgt).
    assert report.summary["added"] == 1
    assert report.summary["removed"] == 1
    assert report.summary["changed"] == 0


def test_report_carries_snapshots_and_timestamp():
    src = _state({"k1": _obj("k1")})
    tgt = _state({"k1": _obj("k1")})
    report = compare(src, tgt)
    assert report.source is src
    assert report.target is tgt
    assert report.generated_at  # ISO timestamp present

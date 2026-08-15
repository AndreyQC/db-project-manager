"""Unit tests for the diff domain models (Phase 9)."""

from db_project_manager.domain.diff import (
    CodebaseManifest,
    DiffEntry,
    DiffReport,
    DiffStatus,
    ObjectSnapshot,
    SnapshotSourceKind,
    StateSnapshot,
)


# --- enums serialize as plain strings ---


def test_diff_status_serializes_as_plain_string():
    assert DiffStatus.ADDED.value == "added"
    assert DiffStatus.REMOVED.value == "removed"
    assert DiffStatus.CHANGED.value == "changed"
    assert DiffStatus.UNCHANGED.value == "unchanged"


def test_snapshot_source_kind_serializes_as_plain_string():
    assert SnapshotSourceKind.DB.value == "db"
    assert SnapshotSourceKind.DIR.value == "dir"


# --- ObjectSnapshot ---


def _obj(**overrides):
    base = {
        "object_key": "pg_database/db/schema/public/type/table/name/t",
        "object_schema": "public",
        "object_name": "t",
        "object_type": "table",
        "sql_normalized": "CREATE TABLE t (a INT)",
        "sql_hash": "abcdef12",
    }
    base.update(overrides)
    return ObjectSnapshot(**base)


def test_object_snapshot_without_estimated_rows_is_valid():
    o = _obj()
    assert o.estimated_rows is None


def test_object_snapshot_with_estimated_rows():
    o = _obj(estimated_rows=12345)
    assert o.estimated_rows == 12345


def test_object_snapshot_roundtrip_json():
    o = _obj(estimated_rows=42, object_signature="deadbeef")
    restored = ObjectSnapshot.model_validate_json(o.model_dump_json())
    assert restored == o
    assert restored.sql_hash == "abcdef12"
    assert restored.estimated_rows == 42


def test_object_snapshot_ignores_extra_fields():
    o = ObjectSnapshot(
        object_key="k",
        object_type="view",
        sql_normalized="SELECT 1",
        sql_hash="00000000",
        unknown_future_field="ignored",
    )
    assert o.object_type == "view"


# --- StateSnapshot ---


def _state(**overrides):
    base = {
        "source_kind": SnapshotSourceKind.DIR,
        "source_ref": "/tmp/codebase",
        "db_type": "postgres",
        "generated_at": "2026-07-28T00:00:00+00:00",
        "objects": {"k": _obj()},
    }
    base.update(overrides)
    return StateSnapshot(**base)


def test_state_snapshot_roundtrip_json():
    s = _state()
    restored = StateSnapshot.model_validate_json(s.model_dump_json())
    assert restored == s
    assert restored.source_kind == SnapshotSourceKind.DIR
    assert "k" in restored.objects


def test_state_snapshot_with_empty_objects():
    s = _state(objects={})
    assert s.objects == {}
    assert StateSnapshot.model_validate_json(s.model_dump_json()).objects == {}


# --- DiffEntry / DiffReport ---


def test_diff_entry_added_has_only_source_snapshot():
    e = DiffEntry(object_key="k", status=DiffStatus.ADDED, source_snapshot=_obj())
    assert e.target_snapshot is None
    assert e.source_snapshot is not None
    # enums serialize as plain strings inside the JSON (str-Enum contract)
    assert '"status":"added"' in e.model_dump_json()
    restored = DiffEntry.model_validate_json(e.model_dump_json())
    assert restored.status == DiffStatus.ADDED


def test_diff_report_roundtrip_json():
    src = _state()
    tgt = _state(source_ref="/tmp/other")
    report = DiffReport(
        source=src,
        target=tgt,
        generated_at="2026-07-28T00:00:00+00:00",
        summary={"added": 0, "removed": 0, "changed": 0, "unchanged": 1},
        entries=[DiffEntry(object_key="k", status=DiffStatus.UNCHANGED)],
    )
    restored = DiffReport.model_validate_json(report.model_dump_json())
    assert restored == report
    assert restored.entries[0].status == DiffStatus.UNCHANGED
    assert restored.summary["unchanged"] == 1


# --- CodebaseManifest ---


def test_codebase_manifest_defaults():
    m = CodebaseManifest(db_type="postgres", database="mydb", generated_at="2026-07-28T00:00:00+00:00")
    assert m.tool_version == ""
    assert m.format_version == 1


def test_codebase_manifest_roundtrip_json():
    m = CodebaseManifest(
        db_type="greenplum",
        database="gpdb",
        generated_at="2026-07-28T00:00:00+00:00",
        tool_version="0.1.0",
    )
    restored = CodebaseManifest.model_validate_json(m.model_dump_json())
    assert restored == m
    assert restored.db_type == "greenplum"

"""Unit tests for the markdown diff report (Phase 14, step S1)."""

from __future__ import annotations

from pathlib import Path

from db_project_manager.domain.diff import (
    DiffEntry,
    DiffReport,
    DiffStatus,
    ObjectSnapshot,
    SnapshotSourceKind,
    StateSnapshot,
)
from db_project_manager.infrastructure.diff.markdown_report import (
    render_diff_markdown,
    unified_diff_text,
    write_diff_markdown,
)


# --- helpers (same pattern as tests/unit/test_comparator.py) ---


def _obj(key: str, sql_hash: str = "aaaa0000", **overrides) -> ObjectSnapshot:
    base = {
        "object_key": key,
        "object_schema": "public",
        "object_name": key.split("name/")[-1] if "name/" in key else key,
        "object_type": "table",
        "sql_normalized": "CREATE TABLE t (a INT)",
        "sql_hash": sql_hash,
    }
    base.update(overrides)
    return ObjectSnapshot(**base)


def _state(objects: dict[str, ObjectSnapshot], **overrides) -> StateSnapshot:
    base = {
        "source_kind": SnapshotSourceKind.DIR,
        "source_ref": "src_dir",
        "db_type": "postgres",
        "generated_at": "2026-07-29T00:00:00+00:00",
        "objects": objects,
    }
    base.update(overrides)
    return StateSnapshot(**base)


def _report(entries: list[DiffEntry], **overrides) -> DiffReport:
    base = {
        "source": _state({}),
        "target": _state({}, source_ref="tgt_dir"),
        "generated_at": "2026-08-04T00:00:00+00:00",
        "summary": {"added": 0, "removed": 0, "changed": 0, "unchanged": 0},
        "entries": entries,
    }
    base.update(overrides)
    return DiffReport(**base)


def _entry(status: DiffStatus, key: str, *, src: ObjectSnapshot | None = None,
           tgt: ObjectSnapshot | None = None) -> DiffEntry:
    if status is DiffStatus.ADDED:
        src = src or _obj(key)
    elif status is DiffStatus.REMOVED:
        tgt = tgt or _obj(key)
    else:
        src = src or _obj(key)
        tgt = tgt or _obj(key)
    return DiffEntry(object_key=key, status=status, source_snapshot=src, target_snapshot=tgt)


# --- summary / header ---


def test_markdown_has_header_and_generated_at():
    report = _report([])
    md = render_diff_markdown(report)
    assert md.startswith("# Diff report")
    assert "2026-08-04T00:00:00+00:00" in md


def test_markdown_source_target_header():
    report = _report([])
    md = render_diff_markdown(report)
    assert "src_dir" in md
    assert "tgt_dir" in md
    # source_kind rendered as the enum value ('dir')
    assert "(dir" in md


def test_markdown_has_summary_table_all_statuses():
    report = _report(
        [
            _entry(DiffStatus.ADDED, "k_added"),
            _entry(DiffStatus.REMOVED, "k_removed"),
            _entry(DiffStatus.CHANGED, "k_changed"),
            _entry(DiffStatus.UNCHANGED, "k_unchanged"),
        ],
        summary={"added": 1, "removed": 1, "changed": 1, "unchanged": 1},
    )
    md = render_diff_markdown(report)
    assert "| status | count |" in md
    for st in ("added", "removed", "changed", "unchanged"):
        assert f"| {st} |" in md


# --- sections ---


def test_markdown_added_section_lists_object():
    report = _report(
        [_entry(DiffStatus.ADDED, "new_table", src=_obj("new_table", object_name="new_table"))],
        summary={"added": 1, "removed": 0, "changed": 0, "unchanged": 0},
    )
    md = render_diff_markdown(report)
    assert "## Added" in md
    assert "`new_table`" in md


def test_markdown_omits_empty_sections_when_only_unchanged():
    report = _report(
        [_entry(DiffStatus.UNCHANGED, "k")],
        summary={"added": 0, "removed": 0, "changed": 0, "unchanged": 1},
    )
    md = render_diff_markdown(report)
    assert "## Added" not in md
    assert "## Removed" not in md
    assert "## Changed" not in md
    # summary still present
    assert "unchanged" in md


def test_markdown_tables_show_rows_and_signature():
    report = _report(
        [
            _entry(
                DiffStatus.ADDED,
                "t1",
                src=_obj(
                    "t1",
                    object_name="t1",
                    object_type="table",
                    object_signature="abc12345",
                    estimated_rows=1234,
                ),
            )
        ],
        summary={"added": 1, "removed": 0, "changed": 0, "unchanged": 0},
    )
    md = render_diff_markdown(report)
    assert "abc12345" in md
    assert "1234" in md


def test_markdown_non_table_omits_rows():
    report = _report(
        [
            _entry(
                DiffStatus.ADDED,
                "v1",
                src=_obj("v1", object_name="v1", object_type="view", estimated_rows=999),
            )
        ],
        summary={"added": 1, "removed": 0, "changed": 0, "unchanged": 0},
    )
    md = render_diff_markdown(report)
    assert "999" not in md


# --- changed diff block ---


def test_markdown_changed_has_collapsible_diff():
    src = _obj("k", object_name="k", sql_normalized="CREATE TABLE k (a INT)")
    tgt = _obj("k", object_name="k", sql_normalized="CREATE TABLE k (a INT, b TEXT)", sql_hash="bbbb0000")
    report = _report(
        [_entry(DiffStatus.CHANGED, "k", src=src, tgt=tgt)],
        summary={"added": 0, "removed": 0, "changed": 1, "unchanged": 0},
    )
    md = render_diff_markdown(report)
    assert "## Changed" in md
    assert "<details>" in md
    assert "</details>" in md
    assert "```diff" in md
    # unified diff markers
    assert "+++" in md  # difflib header for added lines file
    assert "---" in md  # header / or removed-line marker


def test_markdown_changed_identical_sql_omits_diff_block():
    # An edge case: a changed status but equal SQL (shouldn't normally happen — hash compare).
    src = _obj("k", object_name="k", sql_normalized="SELECT 1")
    tgt = _obj("k", object_name="k", sql_normalized="SELECT 1")
    report = _report(
        [_entry(DiffStatus.CHANGED, "k", src=src, tgt=tgt)],
        summary={"added": 0, "removed": 0, "changed": 1, "unchanged": 0},
    )
    md = render_diff_markdown(report)
    assert "<details>" not in md


# --- overloads distinct ---


def test_markdown_overloads_distinct():
    """Two overloads (different object_key, same name) are both listed."""
    key1 = "pg/db/type/function/name/sp_x/signature/aaa"
    key2 = "pg/db/type/function/name/sp_x/signature/bbb"
    report = _report(
        [
            _entry(DiffStatus.ADDED, key1, src=_obj(key1, object_name="sp_x", object_type="function", object_signature="aaa")),
            _entry(DiffStatus.ADDED, key2, src=_obj(key2, object_name="sp_x", object_type="function", object_signature="bbb")),
        ],
        summary={"added": 2, "removed": 0, "changed": 0, "unchanged": 0},
    )
    md = render_diff_markdown(report)
    assert "aaa" in md
    assert "bbb" in md


# --- unified_diff_text helper ---


def test_unified_diff_text_returns_markers_for_changed():
    src = _obj("k", object_name="k", sql_normalized="CREATE TABLE k (a INT)")
    tgt = _obj("k", object_name="k", sql_normalized="CREATE TABLE k (a INT, b TEXT)")
    diff = unified_diff_text(src, tgt)
    assert "@@" in diff
    assert "+" in diff  # added line marker
    assert "-" in diff  # header marker


def test_unified_diff_text_empty_when_identical():
    src = _obj("k", sql_normalized="SELECT 1")
    tgt = _obj("k", sql_normalized="SELECT 1")
    assert unified_diff_text(src, tgt) == ""


# --- write_diff_markdown (I/O) ---


def test_write_diff_markdown_from_path(tmp_path: Path):
    report = _report(
        [_entry(DiffStatus.ADDED, "new_table", src=_obj("new_table", object_name="new_table"))],
        summary={"added": 1, "removed": 0, "changed": 0, "unchanged": 0},
    )
    json_path = tmp_path / "diff_report.json"
    json_path.write_text(report.model_dump_json(), encoding="utf-8")

    out = write_diff_markdown(json_path)
    assert out.name == "diff_report.md"
    assert out.parent == tmp_path
    assert "# Diff report" in out.read_text(encoding="utf-8")


def test_write_diff_markdown_custom_output(tmp_path: Path):
    report = _report([])
    out_path = tmp_path / "custom.md"
    out = write_diff_markdown(report, output=out_path)
    assert out == out_path
    assert out.exists()

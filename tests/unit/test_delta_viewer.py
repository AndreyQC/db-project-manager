"""Offscreen smoke tests for Delta Viewer (Phase 14 S3, LESSONS §41).

The window is constructed and exercised via a real QApplication on the offscreen
platform. ``show_report`` populates the tree synchronously (the async
``load_from_path`` path is covered indirectly via the worker in test_workers).
"""

from __future__ import annotations

import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path  # noqa: E402

import pytest  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from db_project_manager.domain.diff import (  # noqa: E402
    DiffEntry,
    DiffReport,
    DiffStatus,
    ObjectSnapshot,
    SnapshotSourceKind,
    StateSnapshot,
)
from db_project_manager.presentation.gui.widgets.delta_viewer import (  # noqa: E402
    DeltaViewerWindow,
    _IS_OBJECT_ROLE,
    _STATUS_ROLE,
)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


# --- fixtures ---


def _obj(key: str, **overrides) -> ObjectSnapshot:
    base = {
        "object_key": key,
        "object_schema": "public",
        "object_name": key,
        "object_type": "table",
        "sql_normalized": "CREATE TABLE t (a INT)",
        "sql_hash": "aaaa0000",
        "estimated_rows": 100,
    }
    base.update(overrides)
    return ObjectSnapshot(**base)


def _build_report() -> DiffReport:
    src_changed = _obj("k_changed", sql_normalized="CREATE TABLE k (a INT)")
    tgt_changed = _obj("k_changed", sql_normalized="CREATE TABLE k (a INT, b TEXT)", sql_hash="bbbb0000")
    added = _obj("k_added", object_name="new_table")
    removed = _obj("k_removed", object_name="old_table")
    unchanged = _obj("k_unchanged", object_name="same_table")
    return DiffReport(
        source=StateSnapshot(
            source_kind=SnapshotSourceKind.DIR,
            source_ref="src_dir",
            db_type="postgres",
            generated_at="2026-08-04T00:00:00+00:00",
            objects={"k_changed": src_changed, "k_added": added, "k_unchanged": unchanged},
        ),
        target=StateSnapshot(
            source_kind=SnapshotSourceKind.DB,
            source_ref="prod_conn",
            db_type="postgres",
            generated_at="2026-08-04T00:00:00+00:00",
            objects={"k_changed": tgt_changed, "k_removed": removed, "k_unchanged": unchanged},
        ),
        generated_at="2026-08-04T00:00:00+00:00",
        summary={"added": 1, "removed": 1, "changed": 1, "unchanged": 1},
        entries=[
            DiffEntry(object_key="k_changed", status=DiffStatus.CHANGED,
                      source_snapshot=src_changed, target_snapshot=tgt_changed),
            DiffEntry(object_key="k_added", status=DiffStatus.ADDED, source_snapshot=added),
            DiffEntry(object_key="k_removed", status=DiffStatus.REMOVED, target_snapshot=removed),
            DiffEntry(object_key="k_unchanged", status=DiffStatus.UNCHANGED,
                      source_snapshot=unchanged, target_snapshot=unchanged),
        ],
    )


def _object_items(window: DeltaViewerWindow):
    """Yield (leaf_item, entry) for all object leaves in the tree."""
    root = window._tree.invisibleRootItem()
    for i in range(root.childCount()):
        type_node = root.child(i)
        for j in range(type_node.childCount()):
            schema_node = type_node.child(j)
            for k in range(schema_node.childCount()):
                leaf = schema_node.child(k)
                if leaf.data(0, _IS_OBJECT_ROLE):
                    yield leaf


# --- tree population ---


def test_show_report_populates_tree_and_summary(qapp):
    window = DeltaViewerWindow()
    report = _build_report()
    window.show_report(report)

    # Top-level type nodes: only 'table' in this fixture.
    root = window._tree.invisibleRootItem()
    type_texts = [root.child(i).text(0) for i in range(root.childCount())]
    assert any("table" in t for t in type_texts), type_texts

    # Four object leaves total.
    leaves = list(_object_items(window))
    assert len(leaves) == 4

    # Summary bar contains source/target refs and the counts.
    summary = window._summary_label.text()
    assert "src_dir" in summary
    assert "prod_conn" in summary
    assert "added: 1" in summary
    assert "changed: 1" in summary


def test_selecting_changed_object_shows_diff_markers(qapp):
    window = DeltaViewerWindow()
    window.show_report(_build_report())

    # Find the changed leaf and click it.
    changed_leaf = next(
        leaf for leaf in _object_items(window)
        if leaf.data(0, _STATUS_ROLE) == DiffStatus.CHANGED.value
    )
    window._on_tree_clicked(changed_leaf, 0)

    diff_text = window._diff_view.toPlainText()
    assert "@@" in diff_text or "---" in diff_text  # unified-diff header present
    assert "b TEXT" in diff_text  # added column visible


def test_selecting_unchanged_shows_no_diff_message(qapp):
    window = DeltaViewerWindow()
    window.show_report(_build_report())

    unchanged_leaf = next(
        leaf for leaf in _object_items(window)
        if leaf.data(0, _STATUS_ROLE) == DiffStatus.UNCHANGED.value
    )
    window._on_tree_clicked(unchanged_leaf, 0)
    assert "без изменений" in window._diff_view.toPlainText()


def test_selecting_added_shows_no_target_message(qapp):
    window = DeltaViewerWindow()
    window.show_report(_build_report())

    added_leaf = next(
        leaf for leaf in _object_items(window)
        if leaf.data(0, _STATUS_ROLE) == DiffStatus.ADDED.value
    )
    window._on_tree_clicked(added_leaf, 0)
    assert "добавлен" in window._diff_view.toPlainText().lower()


def test_details_populated_for_selected_object(qapp):
    window = DeltaViewerWindow()
    window.show_report(_build_report())

    leaf = next(iter(_object_items(window)))
    window._on_tree_clicked(leaf, 0)
    # Some field other than the placeholder should now be populated.
    assert window._detail_fields["object_type"].text() != "—"


# --- filters ---


def test_filter_hides_unchecked_statuses(qapp):
    window = DeltaViewerWindow()
    window.show_report(_build_report())

    # Uncheck 'unchanged' → all unchanged leaves hidden.
    window._status_checks[DiffStatus.UNCHANGED].setChecked(False)
    window._on_filter_changed()

    for leaf in _object_items(window):
        if leaf.data(0, _STATUS_ROLE) == DiffStatus.UNCHANGED.value:
            assert leaf.isHidden(), "unchanged leaf must be hidden when filter is off"
        else:
            assert not leaf.isHidden()

    # Restore.
    window._status_checks[DiffStatus.UNCHANGED].setChecked(True)
    window._on_filter_changed()
    for leaf in _object_items(window):
        assert not leaf.isHidden()


def test_search_filters_by_name(qapp):
    window = DeltaViewerWindow()
    window.show_report(_build_report())

    window._on_search_changed("new_table")

    for leaf in _object_items(window):
        if "new_table" in leaf.text(0).lower():
            assert not leaf.isHidden()
        else:
            assert leaf.isHidden(), f"non-matching leaf should be hidden: {leaf.text(0)}"


# --- save selection ---


def test_save_selection_writes_json(qapp, tmp_path: Path, monkeypatch):
    window = DeltaViewerWindow()
    report_path = tmp_path / "diff_report.json"
    report_path.write_text(_build_report().model_dump_json(), encoding="utf-8")
    window.show_report(_build_report(), path=report_path)

    # Mark two object leaves as checked.
    leaves = list(_object_items(window))
    leaves[0].setCheckState(0, Qt.CheckState.Checked)
    leaves[1].setCheckState(0, Qt.CheckState.Checked)

    # Suppress the success QMessageBox.
    monkeypatch.setattr(
        "db_project_manager.presentation.gui.widgets.delta_viewer.QMessageBox.information",
        lambda *a, **k: None,
    )
    window._on_save_selection()

    out = tmp_path / "selection.json"
    assert out.is_file()
    data = json.loads(out.read_text(encoding="utf-8"))
    assert len(data["selected"]) == 2


def test_save_selection_nothing_selected_no_file(qapp, tmp_path: Path, monkeypatch):
    window = DeltaViewerWindow()
    window.show_report(_build_report(), path=tmp_path / "diff_report.json")

    monkeypatch.setattr(
        "db_project_manager.presentation.gui.widgets.delta_viewer.QMessageBox.information",
        lambda *a, **k: None,
    )
    window._on_save_selection()
    assert not (tmp_path / "selection.json").exists()


# --- worker integration (async path) ---


def test_load_from_path_populates_report(qapp, tmp_path: Path):
    """The async load path ends with show_report via the worker's finished signal."""
    report_path = tmp_path / "diff_report.json"
    report_path.write_text(_build_report().model_dump_json(), encoding="utf-8")

    window = DeltaViewerWindow()
    window.load_from_path(report_path)
    assert window.thread_pool.waitForDone(10000)
    qapp.processEvents()

    assert window._report is not None
    assert not window._active_workers, "worker must be released after finished"
    # Tree is populated.
    assert window._tree.topLevelItemCount() > 0

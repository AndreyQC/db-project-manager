"""Offscreen smoke tests for the Delta Viewer <-> MainWindow wiring (Phase 14 S4).

Covers the menu entry and the ``open_delta_viewer`` entry point. LESSONS §42: child
windows are held by a strong ref (``_child_windows``) so they are not garbage-collected
mid-use; the ref is dropped when the window is destroyed.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from db_project_manager.domain.diff import (  # noqa: E402
    DiffEntry,
    DiffReport,
    DiffStatus,
    ObjectSnapshot,
    SnapshotSourceKind,
    StateSnapshot,
)
from db_project_manager.infrastructure.config.app_config import CFG
from db_project_manager.presentation.gui.main_window import MainWindow  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _synthetic_report() -> DiffReport:
    obj = ObjectSnapshot(
        object_key="k1",
        object_schema="public",
        object_name="t1",
        object_type="table",
        sql_normalized="CREATE TABLE t1 (a INT)",
        sql_hash="aaaa0000",
    )
    return DiffReport(
        source=StateSnapshot(
            source_kind=SnapshotSourceKind.DIR,
            source_ref="src",
            db_type="postgres",
            generated_at="2026-08-04T00:00:00+00:00",
            objects={"k1": obj},
        ),
        target=StateSnapshot(
            source_kind=SnapshotSourceKind.DIR,
            source_ref="tgt",
            db_type="postgres",
            generated_at="2026-08-04T00:00:00+00:00",
            objects={},
        ),
        generated_at="2026-08-04T00:00:00+00:00",
        summary={"added": 1, "removed": 0, "changed": 0, "unchanged": 0},
        entries=[DiffEntry(object_key="k1", status=DiffStatus.ADDED, source_snapshot=obj)],
    )


def test_main_window_has_delta_viewer_menu_action(qapp, tmp_path, monkeypatch):
    """The View menu exposes a 'Delta Viewer...' action."""
    monkeypatch.chdir(tmp_path)
    window = MainWindow(cfg=CFG())
    assert window._delta_viewer_action.text().startswith("Delta Viewer")


def test_open_delta_viewer_creates_and_holds_window(qapp, tmp_path, monkeypatch):
    """open_delta_viewer creates a Delta Viewer window and keeps a strong ref to it."""
    monkeypatch.chdir(tmp_path)
    window = MainWindow(cfg=CFG())
    report_path = tmp_path / "diff_report.json"
    report_path.write_text(_synthetic_report().model_dump_json(), encoding="utf-8")

    assert not window._child_windows
    window.open_delta_viewer(str(report_path))

    assert len(window._child_windows) == 1
    dv = window._child_windows[0]
    assert dv.isVisible()

    # Let the load worker finish and populate the tree.
    assert window.thread_pool.waitForDone(10000)
    qapp.processEvents()
    assert dv._report is not None
    assert dv._tree.topLevelItemCount() > 0


def test_child_window_ref_released_on_destroy(qapp, tmp_path, monkeypatch):
    """When a child window is destroyed, its strong ref is dropped (LESSONS §42)."""
    monkeypatch.chdir(tmp_path)
    window = MainWindow(cfg=CFG())
    window.open_delta_viewer()
    dv = window._child_windows[0]
    assert dv in window._child_windows

    window._on_child_window_closed(dv)
    assert dv not in window._child_windows

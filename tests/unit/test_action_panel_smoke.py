"""Offscreen smoke tests for the GUI action panel (QT_QPA_PLATFORM=offscreen).

Regression coverage for two Phase 7 bugs:
1. After a successful run the panel stayed blocked: 'finished' was connected to
   a lambda that PySide6 only weakly references -> garbage-collected before the
   signal fired. Now a bound method + strong worker refs are used.
2. OK/Cancel buttons appeared mid-dialog: BaseActionDialog added the button box
   before subclasses appended their fields. Now _add_buttons() is called last.

Widgets are exercised via a real QApplication on the offscreen platform.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialogButtonBox  # noqa: E402

from db_project_manager.infrastructure.config.app_config import CFG  # noqa: E402
from db_project_manager.infrastructure.config.connection_store import (  # noqa: E402
    ConnectionStore,
)
from db_project_manager.infrastructure.config.gui_settings import GuiSettingsStore  # noqa: E402
from db_project_manager.presentation.gui.actions.dialogs import (  # noqa: E402
    DeployValidateDialog,
    GraphPrepareDialog,
    ReverseEngineerDialog,
)
from db_project_manager.presentation.gui.actions.models import (  # noqa: E402
    DeployValidateSettings,
    GraphPrepareSettings,
    ReverseEngineerSettings,
)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _last_form_widget(dialog):
    layout = dialog.layout()
    return layout.itemAt(layout.count() - 1).widget()


def test_buttons_are_last_row_reverse_engineer(qapp, tmp_path):
    dlg = ReverseEngineerDialog(ConnectionStore(tmp_path), ReverseEngineerSettings())
    assert isinstance(_last_form_widget(dlg), QDialogButtonBox)


def test_buttons_are_last_row_deploy_validate(qapp, tmp_path):
    dlg = DeployValidateDialog(ConnectionStore(tmp_path), DeployValidateSettings())
    assert isinstance(_last_form_widget(dlg), QDialogButtonBox)


def test_buttons_are_last_row_graph_prepare(qapp, tmp_path):
    dlg = GraphPrepareDialog(ConnectionStore(tmp_path), GraphPrepareSettings())
    assert isinstance(_last_form_widget(dlg), QDialogButtonBox)


def test_panel_unblocked_after_run(qapp, tmp_path, monkeypatch):
    """graph_prepare run to completion must re-enable the panel (bug 1)."""
    from db_project_manager.presentation.gui.main_window import MainWindow

    monkeypatch.chdir(tmp_path)  # keep connections/ and gui_settings.json local
    codebase = tmp_path / "codebase"
    codebase.mkdir()
    # Pre-seed settings so the run uses the temp codebase (as if «Настроить…»
    # had been confirmed earlier).
    GuiSettingsStore(tmp_path / "gui_settings.json").save_action_settings(
        "graph_prepare",
        {"codebase_dir": str(codebase), "format": "graphml", "validate_graph": True},
    )

    window = MainWindow(cfg=CFG())
    panel = window.action_panel
    panel.action_combo.setCurrentIndex(2)  # graph_prepare

    panel._on_run()
    assert not panel.action_combo.isEnabled()  # running -> blocked
    assert window._active_workers, "worker must be referenced until finished"

    assert window.thread_pool.waitForDone(10000)
    qapp.processEvents()

    assert panel.action_combo.isEnabled(), "panel must be unblocked after finished"
    assert panel.run_btn.isEnabled()
    assert not window._active_workers, "finished workers must be released"

    settings = GuiSettingsStore(tmp_path / "gui_settings.json")
    assert settings.get_last_action() == "graph_prepare"
    assert settings.get_action_settings("graph_prepare")["codebase_dir"] == str(codebase)
    assert (codebase / ".dbm_graph" / "graph.graphml").exists()


def test_graph_export_custom_output_dir(qapp, tmp_path):
    """GraphBuildWorker writes the export to output_dir when set (not .dbm_graph)."""
    from db_project_manager.presentation.gui.widgets.workers import GraphBuildWorker

    codebase = tmp_path / "codebase"
    codebase.mkdir()
    export_dir = tmp_path / "graphs"

    worker = GraphBuildWorker(codebase, fmt="graphml", validate=True, output_dir=export_dir)
    results: list = []
    worker.signals.finished.connect(lambda result: results.append(result))
    worker.run()

    assert results and results[0] == export_dir / "graph.graphml"
    assert (export_dir / "graph.graphml").exists()
    assert not (codebase / ".dbm_graph" / "graph.graphml").exists()

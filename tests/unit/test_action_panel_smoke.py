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
    CompareDialog,
    DeployAnalyzeDialog,
    DeployApplyDialog,
    DeployInitServiceSchemaDialog,
    DeployPlanDialog,
    DeployValidateDialog,
    GraphPrepareDialog,
    ReverseEngineerDialog,
)
from db_project_manager.presentation.gui.actions.models import (  # noqa: E402
    CompareSettings,
    DeployAnalyzeSettings,
    DeployApplySettings,
    DeployInitServiceSchemaSettings,
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
    # Robust to registry order changes (Phase 11 inserted deploy_analyze at
    # index 2): look the action up by id instead of hardcoding the index.
    panel.action_combo.setCurrentIndex(panel.action_combo.findData("graph_prepare"))

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


# --- Phase 15: deploy plan / apply dialogs (PRE-2 preflight) ---


def test_buttons_are_last_row_deploy_plan(qapp, tmp_path):
    dlg = DeployPlanDialog(ConnectionStore(tmp_path), DeployApplySettings())
    assert isinstance(_last_form_widget(dlg), QDialogButtonBox)


def test_buttons_are_last_row_deploy_apply(qapp, tmp_path):
    dlg = DeployApplyDialog(ConnectionStore(tmp_path), DeployApplySettings())
    assert isinstance(_last_form_widget(dlg), QDialogButtonBox)


def test_apply_dialog_confirm_checkbox_gates_ok(qapp, tmp_path):
    """PRE-2: OK button is disabled until the user checks the confirmation box.

    Regression for the preflight pattern — without this gate, an accidental click
    on «Применить» would mutate a live database.
    """
    dlg = DeployApplyDialog(ConnectionStore(tmp_path), DeployApplySettings())
    ok_button = dlg._button_box.button(QDialogButtonBox.StandardButton.Ok)
    assert ok_button is not None
    assert ok_button.isEnabled() is False, "OK must start disabled"
    dlg._confirm.setChecked(True)
    qapp.processEvents()
    assert ok_button.isEnabled() is True, "OK must enable after confirmation"
    dlg._confirm.setChecked(False)
    qapp.processEvents()
    assert ok_button.isEnabled() is False, "OK must disable again if user unchecks"


def test_apply_dialog_settings_roundtrip_includes_risk_flag(qapp, tmp_path):
    """``confirm_understands_risk`` must roundtrip through ``settings()``."""
    dlg = DeployApplyDialog(ConnectionStore(tmp_path), DeployApplySettings())
    dlg._confirm.setChecked(True)
    s = dlg.settings()
    assert s.confirm_understands_risk is True


def test_buttons_are_last_row_compare(qapp, tmp_path):
    """CompareDialog must keep buttons as the last row (lesson §43)."""
    dlg = CompareDialog(ConnectionStore(tmp_path), CompareSettings())
    assert isinstance(_last_form_widget(dlg), QDialogButtonBox)


# --- deploy analyze (Phase 11, SG-7) ---


def test_buttons_are_last_row_deploy_analyze(qapp, tmp_path):
    """DeployAnalyzeDialog must keep buttons as the last row (lesson §43)."""
    dlg = DeployAnalyzeDialog(ConnectionStore(tmp_path), DeployAnalyzeSettings())
    assert isinstance(_last_form_widget(dlg), QDialogButtonBox)


def test_deploy_analyze_dialog_settings_roundtrip(qapp, tmp_path):
    """settings() must echo back what the dialog was prefilled with."""
    from db_project_manager.domain.connection import ConnectionConfig

    store = ConnectionStore(tmp_path)
    store.save(
        ConnectionConfig(
            host="h", port=5432, database="prod", username="u", password="p", name="prod"
        ),
        crypto_env="ENVOS_CRYPTO_01",
    )
    settings = DeployAnalyzeSettings(
        codebase_dir="C:/code", target_connection="prod", output_dir="C:/reports"
    )
    dlg = DeployAnalyzeDialog(store, settings)
    restored = dlg.settings()
    assert restored == settings


def test_deploy_analyze_in_registry():
    """The action must be registered with all four factories wired."""
    from db_project_manager.presentation.gui.actions.registry import ACTIONS, get_action

    spec = get_action("deploy_analyze")
    assert spec in ACTIONS
    assert spec.settings_model.__name__ == "DeployAnalyzeSettings"
    assert spec.required_fields == ("codebase_dir", "target_connection", "output_dir")
    assert spec.make_dialog and spec.make_worker and spec.build_cli


def test_deploy_analyze_worker_emits_verdict(qapp, tmp_path, monkeypatch):
    """Worker contract (lesson §42): finished carries the verdict; a hard error
    goes through signals.error + finished(None)."""
    from db_project_manager.domain.safety import SafetyGateVerdict
    from db_project_manager.presentation.gui.widgets.workers import DeployAnalyzeWorker

    import db_project_manager.application.safety_gate_service as sg_module

    verdict = SafetyGateVerdict(clean=True, db_type="postgres", touched=[])
    monkeypatch.setattr(
        sg_module, "SafetyGateService",
        lambda: type("S", (), {"analyze": staticmethod(lambda *a, **k: verdict)})(),
    )
    worker = DeployAnalyzeWorker(object(), tmp_path, tmp_path / "report")
    finished: list = []
    errors: list = []
    worker.signals.finished.connect(finished.append)
    worker.signals.error.connect(errors.append)
    worker.run()
    assert finished == [verdict]
    assert errors == []


def test_deploy_analyze_worker_error_contract(qapp, tmp_path, monkeypatch):
    from db_project_manager.application.safety_gate_service import SafetyGateError
    from db_project_manager.presentation.gui.widgets.workers import DeployAnalyzeWorker

    import db_project_manager.application.safety_gate_service as sg_module

    def _raise(*a, **k):
        raise SafetyGateError("target newer than source")

    monkeypatch.setattr(
        sg_module, "SafetyGateService",
        lambda: type("S", (), {"analyze": staticmethod(_raise)})(),
    )
    worker = DeployAnalyzeWorker(object(), tmp_path, tmp_path / "report")
    finished: list = []
    errors: list = []
    worker.signals.finished.connect(finished.append)
    worker.signals.error.connect(errors.append)
    worker.run()
    assert finished == [None]
    assert errors and "target newer" in errors[0]


def test_compare_connection_combo_has_empty_placeholder(qapp, tmp_path):
    """Regression: with connections present, a side's combo must offer a "(каталог)"
    placeholder so filling the directory field does not violate the connection/dir XOR.

    Before the fix, _connections_combo defaulted to index 0 (first connection) →
    "_side_spec: указаны и подключение, и каталог" error when the user picked a dir.
    """
    store = ConnectionStore(tmp_path)
    # Seed two connections so the combo is non-empty (the bug only manifests then).
    store.save(
        # Minimal ConnectionConfig; password must be encrypted via the store API.
        __import__(
            "db_project_manager.domain.connection", fromlist=["ConnectionConfig"]
        ).ConnectionConfig(
            host="h", port=5432, database="a", username="u", password="p", name="conn_a"
        ),
        crypto_env="ENVOS_CRYPTO_01",
    )
    store.save(
        __import__(
            "db_project_manager.domain.connection", fromlist=["ConnectionConfig"]
        ).ConnectionConfig(
            host="h", port=5432, database="b", username="u", password="p", name="conn_b"
        ),
        crypto_env="ENVOS_CRYPTO_01",
    )

    dlg = CompareDialog(store, CompareSettings())
    # First entry is the placeholder; its userData is "" (read back as no connection).
    assert dlg._source_connection.itemText(0) == "(каталог вместо подключения)"
    assert dlg._source_connection.itemData(0) == ""
    # And it is selected by default (not the first real connection).
    assert dlg._source_connection.currentIndex() == 0
    assert dlg._source_connection.currentData() == ""

    # Filling the directory field and reading settings → no connection set.
    dlg._source_dir.setText("/some/dir")
    s = dlg.settings()
    assert s.source_connection == ""
    assert s.source_dir == "/some/dir"


def test_compare_worker_reports_error_on_missing_side(qapp, tmp_path):
    """CompareWorker surfaces a CompareError when a side is misconfigured.

    Builds two DIR sides pointing at non-existent dirs (no manifest) → CompareService
    raises CompareError → worker emits error + finished(None).
    """
    from db_project_manager.application.compare_service import SideSpec
    from db_project_manager.domain.diff import SnapshotSourceKind
    from db_project_manager.presentation.gui.widgets.workers import CompareWorker

    src = SideSpec(SnapshotSourceKind.DIR, str(tmp_path / "nope_src"))
    tgt = SideSpec(SnapshotSourceKind.DIR, str(tmp_path / "nope_tgt"))
    worker = CompareWorker(src, tgt, tmp_path / "out")
    errors: list[str] = []
    finishes: list = []
    worker.signals.error.connect(lambda msg: errors.append(msg))
    worker.signals.finished.connect(lambda result: finishes.append(result))
    worker.run()

    assert errors, "worker must emit an error on missing manifest"
    assert "не содержит" in errors[0] or "manifest" in errors[0].lower()
    assert finishes == [None]


# --- Phase 15.5.2: deploy init-service-schema dialog ---


def test_buttons_are_last_row_deploy_init_service_schema(qapp, tmp_path):
    """Pre-flight regression for LESSONS §43: ok/cancel must stay at form bottom."""
    dlg = DeployInitServiceSchemaDialog(
        ConnectionStore(tmp_path), DeployInitServiceSchemaSettings()
    )
    assert isinstance(_last_form_widget(dlg), QDialogButtonBox)


def test_init_service_schema_dialog_roundtrip_target_connection(qapp, tmp_path):
    """target_connection round-trips through ``settings()`` unchanged."""
    dlg = DeployInitServiceSchemaDialog(
        ConnectionStore(tmp_path), DeployInitServiceSchemaSettings()
    )
    # Default combo is empty when no connections stored.
    s = dlg.settings()
    assert s.target_connection == ""
    # Verify the field exists in the model (contract guard).
    assert "target_connection" in DeployInitServiceSchemaSettings.model_fields

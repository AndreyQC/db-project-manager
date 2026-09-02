"""Offscreen smoke tests for the Plan Viewer window (QT_QPA_PLATFORM=offscreen).

Phase 15, PRE-1. Mirrors :mod:`tests.unit.test_delta_viewer`: parse a synthetic
``DeltaPlan``, populate the tree, exercise filters and the «Применить» enable
gate. No real database / network access — the viewer is a read-only display.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from db_project_manager.domain.delta import (  # noqa: E402
    DeltaPlan,
    OperationClass,
    PlannedOperation,
)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _plan(operations: list[PlannedOperation]) -> DeltaPlan:
    return DeltaPlan(
        db_type="postgres",
        source_version="src1",
        target_version="tgt1",
        operations=operations,
    )


def _op(name: str, classification: OperationClass, *, script_file: str = "") -> PlannedOperation:
    return PlannedOperation(
        object_key=f"pg/table/{name}",
        object_type="table",
        object_schema="public",
        object_name=name,
        action="alter",
        classification=classification,
        reason=f"reason for {name}",
        script_file=script_file,
    )


def _open_viewer(qapp, plan: DeltaPlan, *, tmp_path, target_connection: str = "", output_dir: str = ""):
    """Create a PlanViewerWindow with a temp ConnectionStore and seed it."""
    from db_project_manager.infrastructure.config.connection_store import ConnectionStore
    from db_project_manager.presentation.gui.widgets.plan_viewer import PlanViewerWindow

    store = ConnectionStore(tmp_path / "connections")
    window = PlanViewerWindow(
        connection_store=store,
        target_connection=target_connection,
        codebase_dir=str(tmp_path / "code"),
        output_dir=output_dir,
        parent=None,
    )
    window.show_plan(plan, path=tmp_path / "plan.json")
    qapp.processEvents()
    return window, store


def test_show_plan_populates_tree_with_three_classifications(qapp, tmp_path):
    plan = _plan(
        [
            _op("safe_t", OperationClass.SAFE),
            _op("needs_pre_t", OperationClass.NEEDS_PRE),
            _op("blocked_t", OperationClass.BLOCKED),
        ]
    )
    window, _store = _open_viewer(qapp, plan, tmp_path=tmp_path)

    root = window._tree.invisibleRootItem()
    assert root.childCount() == 1  # one type: table
    type_node = root.child(0)
    assert type_node.childCount() == 1  # one schema: public
    schema_node = type_node.child(0)
    assert schema_node.childCount() == 3  # three operations

    # Summary bar reports the counts.
    assert "safe: 1" in window._summary_label.text()
    assert "needs-pre: 1" in window._summary_label.text()
    assert "blocked: 1" in window._summary_label.text()


def test_filters_hide_by_classification(qapp, tmp_path):
    plan = _plan(
        [
            _op("safe_t", OperationClass.SAFE),
            _op("blocked_t", OperationClass.BLOCKED),
        ]
    )
    window, _store = _open_viewer(qapp, plan, tmp_path=tmp_path)

    # Uncheck the safe action and re-run the filter handler. A QToolBar click
    # in a real run toggles the action and fires the signal — but the offscreen
    # test bypasses the click, so we drive the handler explicitly. The contract
    # under test is ``_apply_filters`` honoring the latest ``_class_filter``.
    window._class_actions[OperationClass.SAFE].setChecked(False)
    window._on_filter_changed()
    qapp.processEvents()
    assert window._class_filter[OperationClass.SAFE.value] is False
    root = window._tree.invisibleRootItem()
    schema_node = root.child(0).child(0)
    visible_names = [
        schema_node.child(i).text(0) for i in range(schema_node.childCount())
        if not schema_node.child(i).isHidden()
    ]
    assert visible_names == ["blocked_t"]


def test_apply_button_disabled_without_target_connection(qapp, tmp_path):
    plan = _plan([_op("safe_t", OperationClass.SAFE)])
    window, _store = _open_viewer(qapp, plan, tmp_path=tmp_path, target_connection="")
    assert window._apply_act.isEnabled() is False

    window.set_prefill(target_connection="prod")
    assert window._apply_act.isEnabled() is True


def test_show_ddl_reads_script_file(qapp, tmp_path):
    """When script_file exists next to plan.json, the DDL tab shows its content."""
    plan = _plan(
        [_op("safe_t", OperationClass.SAFE, script_file="delta/001_table_public_safe_t.sql")]
    )
    plan_path = tmp_path / "plan.json"
    delta_dir = tmp_path / "delta"
    delta_dir.mkdir()
    sql_path = delta_dir / "001_table_public_safe_t.sql"
    sql_path.write_text("-- example DDL\nALTER TABLE public.safe_t ADD COLUMN c int;\n", encoding="utf-8")
    # Make sure the DDL loader looks at tmp_path as the plan directory.
    from db_project_manager.infrastructure.config.connection_store import ConnectionStore
    from db_project_manager.presentation.gui.widgets.plan_viewer import PlanViewerWindow

    store = ConnectionStore(tmp_path / "connections")
    window = PlanViewerWindow(
        connection_store=store,
        target_connection="prod",
        codebase_dir=str(tmp_path / "code"),
        output_dir=str(tmp_path),
        parent=None,
    )
    # Patch _plan_path so the DDL resolver looks in tmp_path.
    window._plan_path = plan_path
    window.show_plan(plan, path=plan_path)
    qapp.processEvents()

    leaf = window._tree.invisibleRootItem().child(0).child(0).child(0)
    window._on_tree_clicked(leaf, 0)
    qapp.processEvents()
    assert "ALTER TABLE public.safe_t ADD COLUMN c int;" in window._ddl_view.toPlainText()


def test_show_ddl_missing_script_file_shows_placeholder(qapp, tmp_path):
    plan = _plan([_op("safe_t", OperationClass.SAFE, script_file="delta/does_not_exist.sql")])
    from db_project_manager.infrastructure.config.connection_store import ConnectionStore
    from db_project_manager.presentation.gui.widgets.plan_viewer import PlanViewerWindow

    store = ConnectionStore(tmp_path / "connections")
    window = PlanViewerWindow(
        connection_store=store, target_connection="", parent=None
    )
    window.show_plan(plan, path=tmp_path / "plan.json")
    qapp.processEvents()

    leaf = window._tree.invisibleRootItem().child(0).child(0).child(0)
    window._on_tree_clicked(leaf, 0)
    qapp.processEvents()
    assert "файл не найден" in window._ddl_view.toPlainText()


def test_load_plan_report_roundtrip(qapp, tmp_path):
    """``load_plan_report`` parses a plan.json produced by ``write_plan_report``."""
    from db_project_manager.infrastructure.deploy.plan_report import (
        load_plan_report,
        write_plan_report,
    )

    plan = _plan(
        [
            _op("a", OperationClass.SAFE),
            _op("b", OperationClass.NEEDS_PRE),
        ]
    )
    out = tmp_path / "out"
    paths = write_plan_report(plan, out)
    assert (out / "plan.json").exists()
    assert (out / "plan.md").exists()

    loaded = load_plan_report(out / "plan.json")
    assert loaded.db_type == "postgres"
    assert len(loaded.operations) == 2
    assert loaded.safe_ops[0].object_name == "a"
    assert loaded.needs_pre_ops[0].object_name == "b"
    assert paths == [out / "plan.md", out / "plan.json"]


def test_load_plan_report_bad_json_emits_error(qapp, tmp_path):
    """``LoadPlanReportWorker.run`` emits error+finished(None) on bad JSON."""
    from db_project_manager.presentation.gui.widgets.workers import LoadPlanReportWorker

    bad = tmp_path / "plan.json"
    bad.write_text("not json {", encoding="utf-8")
    worker = LoadPlanReportWorker(bad)
    errors: list[str] = []
    finished: list[object] = []
    worker.signals.error.connect(errors.append)
    worker.signals.finished.connect(finished.append)
    worker.run()
    assert errors and finished == [None]


def test_load_plan_report_missing_file_emits_error(qapp, tmp_path):
    from db_project_manager.presentation.gui.widgets.workers import LoadPlanReportWorker

    worker = LoadPlanReportWorker(tmp_path / "does_not_exist.json")
    errors: list[str] = []
    finished: list[object] = []
    worker.signals.error.connect(errors.append)
    worker.signals.finished.connect(finished.append)
    worker.run()
    assert errors and finished == [None]
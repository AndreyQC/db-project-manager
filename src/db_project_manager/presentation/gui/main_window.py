"""Main window — thin presentation layer.

Holds the connection list (add/edit/delete), the action panel (dropdown of
registered actions with configure/run/copy-CLI), a status log, a progress bar,
and the read-only project viewer. All business logic lives in application
services; this class only wires UI to signals.
"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel
from PySide6.QtCore import QThreadPool
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from db_project_manager.infrastructure.config.app_config import CFG, load_cfg
from db_project_manager.infrastructure.config.connection_store import (
    ConnectionStore,
    ConnectionStoreError,
)
from db_project_manager.infrastructure.config.gui_settings import GuiSettingsStore
from db_project_manager.infrastructure.logging_setup import configure as configure_logging
from db_project_manager.presentation.gui.actions.registry import get_action
from db_project_manager.presentation.gui.widgets.action_panel import ActionPanelWidget
from db_project_manager.presentation.gui.widgets.connection_dialog import ConnectionDialog
from db_project_manager.presentation.gui.widgets.connection_list import ConnectionListWidget
from db_project_manager.presentation.gui.widgets.project_viewer import ProjectViewer


class MainWindow(QMainWindow):
    def __init__(self, cfg: CFG | None = None) -> None:
        super().__init__()
        self.cfg = cfg or load_cfg()
        configure_logging(level=self.cfg.logging.level, logs_dir=self.cfg.paths.logs_dir)

        self.store = ConnectionStore()
        self.settings_store = GuiSettingsStore()
        self.thread_pool = QThreadPool.globalInstance()
        # Strong refs to running workers: QThreadPool owns the C++ QRunnable,
        # but the Python wrapper (and its signals object) must stay alive until
        # 'finished' is delivered, otherwise the slot never fires.
        self._active_workers: dict = {}
        # Strong refs to non-modal child windows (Delta Viewer) — without this PySide6
        # would garbage-collect the Python wrapper once the local goes out of scope,
        # closing the window immediately. Released in _on_child_window_closed.
        self._child_windows: list = []

        self.setWindowTitle("DB Project Manager")
        self.setMinimumSize(900, 650)
        self._init_ui()
        self._init_menu()
        self._refresh_connections()
        # Point viewer at the default output dir if it exists.
        self._viewer.set_root(self.cfg.paths.default_output_dir)

    # --- UI construction ---

    def _init_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        # Top row: connections + action panel.
        top = QHBoxLayout()

        connections_group = QGroupBox("Подключения")
        clayout = QVBoxLayout(connections_group)
        self.connection_list = ConnectionListWidget()
        self.connection_list.add_btn.clicked.connect(self._on_add_connection)
        self.connection_list.edit_btn.clicked.connect(self._on_edit_connection)
        self.connection_list.delete_btn.clicked.connect(self._on_delete_connection)
        self.connection_list.view.doubleClicked.connect(self._on_edit_connection)
        clayout.addWidget(self.connection_list)
        top.addWidget(connections_group)

        self.action_panel = ActionPanelWidget(
            self.store,
            self.settings_store,
            self.cfg,
            get_selected_connection=self.connection_list.selected_name,
        )
        self.action_panel.execute_requested.connect(self._on_execute)
        self.action_panel.status_message.connect(self._append_status)
        top.addWidget(self.action_panel, stretch=1)

        root.addLayout(top, stretch=0)

        # Progress + status.
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        root.addWidget(self.progress_bar)
        self.status_edit = QTextEdit()
        self.status_edit.setReadOnly(True)
        self.status_edit.setFixedHeight(110)
        root.addWidget(self.status_edit)

        # Project viewer.
        viewer_group = QGroupBox("Проект базы данных")
        vlayout = QVBoxLayout(viewer_group)
        self._viewer = ProjectViewer()
        vlayout.addWidget(self._viewer)
        root.addWidget(viewer_group, stretch=1)

    def _init_menu(self) -> None:
        """Build the menu bar (Phase 14: View -> Delta Viewer; Phase 15: View -> Plan Viewer)."""
        view_menu = self.menuBar().addMenu("Вид")
        self._delta_viewer_action = QAction("Delta Viewer…", self)
        self._delta_viewer_action.setToolTip("Открыть отчёт сравнения для просмотра")
        self._delta_viewer_action.triggered.connect(self._on_open_delta_viewer)
        view_menu.addAction(self._delta_viewer_action)

        self._plan_viewer_action = QAction("Plan Viewer…", self)
        self._plan_viewer_action.setToolTip("Открыть план деплоя (plan.json) для просмотра")
        self._plan_viewer_action.triggered.connect(self._on_open_plan_viewer)
        view_menu.addAction(self._plan_viewer_action)

    # --- Delta Viewer (Phase 14) ---

    def open_delta_viewer(self, report_path: str | None = None) -> None:
        """Open the Delta Viewer window, optionally loading a report right away.

        Factored out of :meth:`_on_open_delta_viewer` so tests can drive it with a
        known path (and skip the file dialog). ``report_path=None`` opens the window
        empty; the user then picks a JSON via the window's own toolbar.
        """
        from db_project_manager.presentation.gui.widgets.delta_viewer import DeltaViewerWindow

        window = DeltaViewerWindow(parent=self)
        # Release the strong ref when the window closes — otherwise we leak closed windows.
        window.destroyed.connect(lambda _obj=None: self._on_child_window_closed(window))
        self._child_windows.append(window)
        if report_path is not None:
            window.load_from_path(report_path)
        window.show()

    def _on_open_delta_viewer(self) -> None:
        """Menu handler: ask for a diff_report.json, then open the Delta Viewer on it."""
        from PySide6.QtWidgets import QFileDialog

        path, _ = QFileDialog.getOpenFileName(
            self, "Открыть diff_report.json", "", "JSON (*.json)"
        )
        if not path:
            return
        self.open_delta_viewer(path)

    # --- Plan Viewer (Phase 15) ---

    def open_plan_viewer(
        self,
        report_path: str | None = None,
        *,
        target_connection: str = "",
        codebase_dir: str = "",
        output_dir: str = "",
    ) -> None:
        """Open the Plan Viewer window, optionally pre-filled (Phase 15, PRE-1).

        When ``target_connection``/``codebase_dir``/``output_dir`` are supplied
        (e.g. after a successful ``deploy plan`` or ``deploy apply``), the
        viewer's «Применить» toolbar action is enabled — it can launch a
        :class:`DeployApplyDialog` with these defaults (PRE-3).
        """
        from db_project_manager.presentation.gui.widgets.plan_viewer import PlanViewerWindow

        window = PlanViewerWindow(
            connection_store=self.store,
            target_connection=target_connection,
            codebase_dir=codebase_dir,
            output_dir=output_dir,
            parent=self,
        )
        window.destroyed.connect(lambda _obj=None: self._on_child_window_closed(window))
        self._child_windows.append(window)
        if report_path is not None:
            window.load_from_path(report_path)
        window.show()

    def _on_open_plan_viewer(self) -> None:
        """Menu handler: ask for a plan.json, then open the Plan Viewer on it."""
        from PySide6.QtWidgets import QFileDialog

        path, _ = QFileDialog.getOpenFileName(
            self, "Открыть plan.json", "", "JSON (*.json)"
        )
        if not path:
            return
        self.open_plan_viewer(path)

    def _on_child_window_closed(self, window) -> None:
        try:
            self._child_windows.remove(window)
        except ValueError:
            pass

    # --- connections ---

    def _refresh_connections(self) -> None:
        self.connection_list.refresh(self.store.list_names())

    def _crypto_env(self) -> str:
        # The env var holding the Fernet key for encrypting passwords.
        return os.environ.get("DBPM_CRYPTO_ENV", "ENVOS_CRYPTO_01")

    def _on_add_connection(self) -> None:
        dlg = ConnectionDialog(self.store, parent=self, crypto_env=self._crypto_env())
        if dlg.exec() == ConnectionDialog.DialogCode.Accepted:
            self._refresh_connections()

    def _on_edit_connection(self) -> None:
        name = self.connection_list.selected_name()
        if not name:
            return
        dlg = ConnectionDialog(self.store, name=name, parent=self, crypto_env=self._crypto_env())
        if dlg.exec() == ConnectionDialog.DialogCode.Accepted:
            self._refresh_connections()

    def _on_delete_connection(self) -> None:
        name = self.connection_list.selected_name()
        if not name:
            return
        reply = QMessageBox.question(
            self, "Удаление", f"Удалить подключение '{name}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.store.delete(name)
            self._refresh_connections()

    # --- action execution ---

    def _on_execute(self, action_id: str, settings: BaseModel) -> None:
        """Build the action's worker and run it on the thread pool."""
        spec = get_action(action_id)
        try:
            worker = spec.make_worker(self.store, settings)
        except ConnectionStoreError as e:
            QMessageBox.critical(self, spec.title, f"Не удалось загрузить подключение: {e}")
            return
        except ValueError as e:
            # Raised by compare's _side_spec when a side has both/neither fields set.
            QMessageBox.critical(self, spec.title, str(e))
            return

        self._set_running(True)
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 0)  # indeterminate until first progress
        self.status_edit.clear()
        self._append_status(f"Запуск: {spec.title}")

        worker.signals.progress.connect(self._on_progress)
        worker.signals.status.connect(self._append_status)
        worker.signals.error.connect(self._on_error)
        # No lambda/partial here: PySide6 holds only a weak ref to such
        # receivers, so a closure created here would be garbage-collected
        # before 'finished' fires. A bound method (self) survives.
        worker.signals.finished.connect(self._on_worker_finished)
        self._active_workers[worker.signals] = (worker, action_id, settings)
        self.thread_pool.start(worker)

    def _on_worker_finished(self, result) -> None:
        entry = self._active_workers.pop(self.sender(), None)
        if entry is None:
            return
        _worker, action_id, settings = entry
        self._on_action_finished(action_id, settings, result)

    def _on_action_finished(self, action_id: str, settings: BaseModel, result) -> None:
        self._set_running(False)
        self.progress_bar.setVisible(False)
        if result is None:
            return  # error already reported via _on_error
        self.action_panel.mark_executed()
        if action_id == "deploy_validate":
            self._report_deploy_result(result)
        elif action_id == "deploy_analyze":
            # result is a SafetyGateVerdict (read-only dry-run, SG-7).
            self._report_analyze_result(result, settings)
        elif action_id == "deploy_plan":
            # result is a DeltaPlan (dry-run, read-only). Offer to open Plan Viewer.
            self._report_plan_result(result, settings)
        elif action_id == "deploy_apply":
            # result is an ApplyResult (mutates target). Offer to open Plan Viewer.
            self._report_apply_result(result, settings)
        elif action_id == "compare":
            # result is the report dir (Path). Open it in the viewer and show a summary.
            self._viewer.set_root(str(result))
            self._append_status(self._compare_summary(result))
        else:
            self._append_status(f"Готово: {result}")
            if action_id == "reverse_engineer":
                self._viewer.set_root(settings.output_dir)

    def _compare_summary(self, report_dir) -> str:
        """Format an added/removed/changed/unchanged line from diff_report.json.

        Best-effort: if the JSON is missing/unreadable, fall back to a plain
        'Готово' line so a transient read failure never hides the successful run.
        """
        import json

        from pathlib import Path

        path = Path(report_dir) / "diff_report.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            summary = data.get("summary", {})
            return (
                f"Сравнение завершено: добавлено {summary.get('added', '?')}, "
                f"удалено {summary.get('removed', '?')}, "
                f"изменено {summary.get('changed', '?')}, "
                f"без изменений {summary.get('unchanged', '?')}. Отчёт: {report_dir}"
            )
        except Exception:  # noqa: BLE001 — best-effort summary, never fatal
            return f"Готово: {report_dir}"

    def _report_analyze_result(self, verdict, settings) -> None:
        """Show the safety-gate verdict + point to the report (Phase 11, SG-7)."""
        from pathlib import Path

        report_md = Path(settings.output_dir) / "safety_gate_report.md"
        touched = len(getattr(verdict, "touched", []) or [])
        violations = getattr(verdict, "violations", []) or []
        if getattr(verdict, "clean", False):
            self._append_status(
                f"✓ Safety gate: CLEAN (тронутых таблиц: {touched}). Отчёт: {report_md}"
            )
            QMessageBox.information(
                self,
                "Safety gate",
                f"✓ Нарушений нет.\nТронутых таблиц: {touched}\nОтчёт: {report_md}",
            )
            return
        self._append_status(
            f"✗ Safety gate: VIOLATIONS ({len(violations)}) — пайплайн остановлен. "
            f"Отчёт: {report_md}"
        )
        for v in violations:
            self._append_status(
                f"  ! {v.object_schema}.{v.name} [{v.touch.value}, "
                f"~{v.estimated_rows} строк] — нет покрывающего pre-скрипта"
            )
        QMessageBox.warning(
            self,
            "Safety gate",
            f"✗ Нарушений: {len(violations)}.\n"
            "Таблицы с данными изменяются без покрывающего pre-скрипта.\n"
            f"Отчёт: {report_md}",
        )

    def _report_deploy_result(self, result) -> None:
        # DeployResult has .success / .db_name / .errors / objects_done/total
        success = getattr(result, "success", False)
        db_name = getattr(result, "db_name", "?")
        done = getattr(result, "objects_done", 0)
        total = getattr(result, "objects_total", 0)
        errors = getattr(result, "errors", []) or []
        if success:
            self._append_status(f"✓ Деплой успешен: база {db_name}, объектов {done}/{total}")
            QMessageBox.information(
                self, "Deploy", f"✓ Успешно.\nБаза: {db_name}\nОбъектов: {done}/{total}"
            )
        else:
            for err in errors:
                self._append_status(
                    f"✗ [{err.object_type}] {err.object_name} ({err.source_file}): {err.error}"
                )
            self._append_status(f"Деплой с ошибками: база {db_name}, объектов {done}/{total}")
            QMessageBox.warning(
                self, "Deploy", f"Деплой завершился с ошибками ({len(errors)}).\nБаза: {db_name}"
            )

    # --- Phase 15: deploy plan / apply result reporting ---

    def _plan_json_path(self, output_dir: str | Path) -> Path:
        return Path(output_dir) / "plan.json"

    def _report_plan_result(self, plan, settings) -> None:
        """Show plan counters + offer to open the Plan Viewer (Phase 15, PRE-3)."""
        plan_path = self._plan_json_path(settings.output_dir)
        ops = len(getattr(plan, "operations", []) or [])
        safe = len(getattr(plan, "safe_ops", []) or [])
        needs_pre = len(getattr(plan, "needs_pre_ops", []) or [])
        blocked = len(getattr(plan, "violations", []) or [])
        self._append_status(
            f"✓ Plan готов: операций {ops} (safe: {safe}, needs-pre: {needs_pre}, "
            f"blocked: {blocked}). Отчёт: {plan_path}"
        )
        body = (
            f"План сформирован: операций {ops}.\n"
            f"safe: {safe}; needs-pre: {needs_pre}; blocked: {blocked}.\n\n"
            f"Открыть план в Plan Viewer?\n\n"
            f"Файл: {plan_path}"
        )
        reply = QMessageBox.question(
            self,
            "Deploy plan",
            body,
            QMessageBox.StandardButton.Open | QMessageBox.StandardButton.Close,
            QMessageBox.StandardButton.Open,
        )
        if reply == QMessageBox.StandardButton.Open and plan_path.exists():
            self.open_plan_viewer(
                str(plan_path),
                target_connection=getattr(settings, "target_connection", "") or "",
                codebase_dir=getattr(settings, "codebase_dir", "") or "",
                output_dir=getattr(settings, "output_dir", "") or "",
            )

    def _report_apply_result(self, result, settings) -> None:
        """Show apply counters + offer to open the Plan Viewer (Phase 15, PRE-3)."""
        plan_path = self._plan_json_path(settings.output_dir)
        applied = getattr(result, "applied", "?")
        planned = getattr(result, "planned", "?")
        applied_version = getattr(result, "applied_version", None) or "—"
        rehearsal_db = getattr(result, "rehearsal_db", None) or "(без репетиции)"
        self._append_status(
            f"✓ Apply завершён: {applied}/{planned} операций, версия {applied_version}, "
            f"репетиция: {rehearsal_db}. Артефакты: {plan_path.parent}"
        )
        body = (
            f"✓ Apply завершён.\n\n"
            f"Применено: {applied}/{planned} операций.\n"
            f"Версия схемы: {applied_version}.\n"
            f"Репетиция: {rehearsal_db}.\n\n"
            f"Открыть план в Plan Viewer?\n\n"
            f"Файл: {plan_path}"
        )
        reply = QMessageBox.question(
            self,
            "Deploy apply",
            body,
            QMessageBox.StandardButton.Open | QMessageBox.StandardButton.Close,
            QMessageBox.StandardButton.Open,
        )
        if reply == QMessageBox.StandardButton.Open and plan_path.exists():
            self.open_plan_viewer(
                str(plan_path),
                target_connection=getattr(settings, "target_connection", "") or "",
                codebase_dir=getattr(settings, "codebase_dir", "") or "",
                output_dir=getattr(settings, "output_dir", "") or "",
            )

    # --- shared worker plumbing ---

    def _on_progress(self, message: str, current: int, total: int) -> None:
        self.progress_bar.setRange(0, total)
        self.progress_bar.setValue(current)

    def _on_error(self, message: str) -> None:
        self._append_status(f"ОШИБКА: {message}")
        QMessageBox.critical(self, "Ошибка", message)

    def _set_running(self, running: bool) -> None:
        self.action_panel.set_running(running)

    def _append_status(self, message: str) -> None:
        self.status_edit.append(message)

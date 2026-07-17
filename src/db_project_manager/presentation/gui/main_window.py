"""Main window — thin presentation layer.

Holds connection list, output directory chooser, the run button, a status log,
a progress bar, and the read-only project viewer. All business logic lives in
application services; this class only wires UI to signals.
"""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import (
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from db_project_manager.infrastructure.config.app_config import CFG, load_cfg
from db_project_manager.infrastructure.config.connection_store import (
    ConnectionStore,
    ConnectionStoreError,
)
from db_project_manager.infrastructure.logging_setup import configure as configure_logging
from db_project_manager.presentation.gui.widgets.connection_dialog import ConnectionDialog
from db_project_manager.presentation.gui.widgets.connection_list import ConnectionListWidget
from db_project_manager.presentation.gui.widgets.project_viewer import ProjectViewer
from db_project_manager.presentation.gui.widgets.workers import ReverseEngineerWorker


class MainWindow(QMainWindow):
    def __init__(self, cfg: CFG | None = None) -> None:
        super().__init__()
        self.cfg = cfg or load_cfg()
        configure_logging(level=self.cfg.logging.level, logs_dir=self.cfg.paths.logs_dir)

        self.store = ConnectionStore()
        self.thread_pool = QThreadPool.globalInstance()

        self.setWindowTitle("DB Project Manager")
        self.setMinimumSize(900, 650)
        self._init_ui()
        self._refresh_connections()
        # Point viewer at the default output dir if it exists.
        self._viewer.set_root(self.cfg.paths.default_output_dir)

    # --- UI construction ---

    def _init_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        # Top row: connections + output dir + run.
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

        actions_group = QGroupBox("Действия")
        alayout = QVBoxLayout(actions_group)

        out_row = QHBoxLayout()
        out_row.addWidget(QLabel("Папка вывода:"))
        self.output_edit = QLineEdit(self.cfg.paths.default_output_dir)
        self.output_edit.setReadOnly(True)
        out_row.addWidget(self.output_edit, stretch=1)
        browse_btn = QPushButton("Выбрать…")
        browse_btn.clicked.connect(self._on_select_output)
        out_row.addWidget(browse_btn)
        alayout.addLayout(out_row)

        self.run_btn = QPushButton("Сгенерировать скрипты объектов БД")
        self.run_btn.clicked.connect(self._on_run)
        alayout.addWidget(self.run_btn)

        top.addWidget(actions_group, stretch=1)
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

    # --- output dir ---

    def _on_select_output(self) -> None:
        start = self.output_edit.text() or str(Path.home())
        directory = QFileDialog.getExistingDirectory(self, "Папка вывода SQL-скриптов", start)
        if directory:
            self.output_edit.setText(directory)
            self.cfg.paths.default_output_dir = directory
            self._viewer.set_root(directory)

    # --- run ---

    def _on_run(self) -> None:
        name = self.connection_list.selected_name()
        if not name:
            QMessageBox.warning(self, "Запуск", "Выберите подключение.")
            return
        try:
            conn_cfg = self.store.load_by_name(name)
        except ConnectionStoreError as e:
            QMessageBox.critical(self, "Запуск", f"Не удалось загрузить подключение: {e}")
            return

        output_dir = self.output_edit.text().strip()
        if not output_dir:
            QMessageBox.warning(self, "Запуск", "Укажите папку вывода.")
            return

        self._set_running(True)
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 4)
        self.progress_bar.setValue(0)
        self.status_edit.clear()
        self._append_status(f"Запуск reverse-engineer: {name}")

        worker = ReverseEngineerWorker(conn_cfg, output_dir)
        worker.signals.progress.connect(self._on_progress)
        worker.signals.status.connect(self._append_status)
        worker.signals.error.connect(self._on_error)
        worker.signals.finished.connect(self._on_finished)
        self.thread_pool.start(worker)

    def _on_progress(self, message: str, current: int, total: int) -> None:
        self.progress_bar.setRange(0, total)
        self.progress_bar.setValue(current)

    def _on_error(self, message: str) -> None:
        self._append_status(f"ОШИБКА: {message}")
        QMessageBox.critical(self, "Ошибка", message)

    def _on_finished(self, result) -> None:
        self._set_running(False)
        self.progress_bar.setVisible(False)
        if result is not None:
            self._append_status(f"Готово: {result}")
            self._viewer.set_root(Path(result).parent if Path(result).parent.exists() else result)

    def _set_running(self, running: bool) -> None:
        self.run_btn.setEnabled(not running)

    def _append_status(self, message: str) -> None:
        self.status_edit.append(message)

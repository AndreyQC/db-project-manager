"""Dialog for creating/editing a database connection.

On accept, the password is encrypted via ConnectionStore.save() before being
written to disk. A "Test" button performs a trial connect through the adapter
without persisting anything.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
)

from db_project_manager.domain.connection import ConnectionConfig
from db_project_manager.infrastructure.config.connection_store import ConnectionStore


class ConnectionDialog(QDialog):
    """Add or edit a connection (UI is the only place connections are created)."""

    DB_TYPES = ("postgres", "greenplum")

    def __init__(
        self,
        store: ConnectionStore,
        *,
        name: str | None = None,
        parent=None,
        crypto_env: str = "ENVOS_CRYPTO_01",
    ) -> None:
        super().__init__(parent)
        self.store = store
        self.crypto_env = crypto_env
        self.original_name = name  # when editing, used to overwrite the same file

        is_edit = name is not None
        self.setWindowTitle("Редактировать подключение" if is_edit else "Добавить подключение")
        self.setMinimumWidth(420)

        self._init_ui()
        if is_edit:
            self._load_existing(name)

    def _init_ui(self) -> None:
        form = QFormLayout(self)

        self.name_edit = QLineEdit()
        self.type_combo = QComboBox()
        self.type_combo.addItems(self.DB_TYPES)
        self.host_edit = QLineEdit()
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(5432)
        self.database_edit = QLineEdit()
        self.username_edit = QLineEdit()
        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)

        form.addRow("Название:", self.name_edit)
        form.addRow("Тип БД:", self.type_combo)
        form.addRow("Хост:", self.host_edit)
        form.addRow("Порт:", self.port_spin)
        form.addRow("База данных:", self.database_edit)
        form.addRow("Пользователь:", self.username_edit)
        form.addRow("Пароль:", self.password_edit)

        # Test button next to the buttons row.
        test_btn = QPushButton("Тест соединения")
        test_btn.clicked.connect(self._on_test)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            Qt.Orientation.Horizontal,
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)

        btn_row = QHBoxLayout()
        btn_row.addWidget(test_btn)
        btn_row.addStretch()
        btn_row.addWidget(buttons)
        form.addRow(btn_row)

    def _load_existing(self, name: str) -> None:
        try:
            cfg = self.store.load_by_name(name)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Ошибка", f"Не удалось загрузить подключение: {e}")
            return
        self.name_edit.setText(name)
        self.type_combo.setCurrentText(cfg.type)
        self.host_edit.setText(cfg.host)
        self.port_spin.setValue(cfg.port)
        self.database_edit.setText(cfg.database)
        self.username_edit.setText(cfg.username)
        self.password_edit.setText(cfg.password)

    def _build_config(self) -> ConnectionConfig | None:
        if not self.name_edit.text().strip():
            QMessageBox.warning(self, "Проверка", "Введите название подключения.")
            return None
        if not self.host_edit.text().strip():
            QMessageBox.warning(self, "Проверка", "Введите хост.")
            return None
        if not self.database_edit.text().strip():
            QMessageBox.warning(self, "Проверка", "Введите имя базы данных.")
            return None
        if not self.username_edit.text().strip():
            QMessageBox.warning(self, "Проверка", "Введите имя пользователя.")
            return None
        return ConnectionConfig(
            name=self.name_edit.text().strip(),
            type=self.type_combo.currentText(),
            host=self.host_edit.text().strip(),
            port=self.port_spin.value(),
            database=self.database_edit.text().strip(),
            username=self.username_edit.text().strip(),
            password=self.password_edit.text(),
        )

    def _on_test(self) -> None:
        cfg = self._build_config()
        if cfg is None:
            return
        ok, message = ConnectionDialog.test_connection(cfg)
        if ok:
            QMessageBox.information(self, "Тест соединения", f"✓ Успешно:\n{message}")
        else:
            QMessageBox.warning(self, "Тест соединения", f"✗ Не удалось:\n{message}")

    def _on_accept(self) -> None:
        cfg = self._build_config()
        if cfg is None:
            return
        try:
            # When editing and the name changed, remove the old file.
            if self.original_name and self.original_name != cfg.name:
                self.store.delete(self.original_name)
            self.store.save(cfg, name=cfg.name, crypto_env=self.crypto_env)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Ошибка", f"Не удалось сохранить подключение: {e}")
            return
        self.accept()

    @staticmethod
    def test_connection(cfg: ConnectionConfig) -> tuple[bool, str]:
        """Trial-connect through the adapter registry. Returns (ok, message)."""
        from db_project_manager.infrastructure.database.base import DatabaseError
        from db_project_manager.infrastructure.database.registry import get_adapter

        adapter = get_adapter(cfg)
        try:
            adapter.connect(cfg)
            host_desc = f"{cfg.host}:{cfg.port}/{cfg.database}"
            return True, f"Подключено к {host_desc}"
        except DatabaseError as e:
            return False, str(e)
        finally:
            adapter.disconnect()

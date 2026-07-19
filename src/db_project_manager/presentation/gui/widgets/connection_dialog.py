"""Dialog for creating/editing a database connection.

On accept, the password is encrypted via ConnectionStore.save() before being
written to disk. A "Test" button performs a trial connect through the adapter
without persisting anything.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from db_project_manager.domain.connection import ConnectionConfig, ConnectionType, SSH_TunnelConfig
from db_project_manager.infrastructure.config.connection_store import ConnectionStore


class ConnectionDialog(QDialog):
    """Add or edit a connection (UI is the only place connections are created)."""

    DB_TYPES = ("postgres", "greenplum")
    CONNECTION_TYPES = (
        ("direct", "Direct PG"),
        ("ssh_tunnel", "SSH Tunnel PG"),
    )

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
        self._original_ssh_pass: str | None = None  # for preserving SSH pass on re-save

        is_edit = name is not None
        self.setWindowTitle("Редактировать подключение" if is_edit else "Добавить подключение")
        self.setMinimumWidth(480)

        self._init_ui()
        if is_edit:
            self._load_existing(name)

    def _init_ui(self) -> None:
        form = QFormLayout(self)

        self.name_edit = QLineEdit()
        self.db_type_combo = QComboBox()
        self.db_type_combo.addItems(self.DB_TYPES)

        # Connection type dropdown (Direct PG / SSH Tunnel PG)
        self.connection_type_combo = QComboBox()
        for value, label in self.CONNECTION_TYPES:
            self.connection_type_combo.addItem(label, value)
        self.connection_type_combo.currentIndexChanged.connect(self._on_connection_type_changed)

        # Standard DB fields
        self.host_edit = QLineEdit()
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(5432)
        self.database_edit = QLineEdit()
        self.username_edit = QLineEdit()
        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)

        # Eye toggle to reveal/hide the password value.
        self.password_toggle = QPushButton()
        self.password_toggle.setCheckable(True)
        self.password_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.password_toggle.setFlat(True)
        self.password_toggle.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.password_toggle.setFixedWidth(28)
        self._set_password_visible(False)
        self.password_toggle.toggled.connect(self._set_password_visible)

        password_row = QWidget()
        password_layout = QHBoxLayout(password_row)
        password_layout.setContentsMargins(0, 0, 0, 0)
        password_layout.addWidget(self.password_edit)
        password_layout.addWidget(self.password_toggle)

        # SSH Tunnel fields (visible only when SSH Tunnel is selected)
        self.ssh_group = QWidget()
        ssh_layout = QVBoxLayout(self.ssh_group)
        ssh_layout.setContentsMargins(0, 0, 0, 0)

        ssh_form = QFormLayout()
        ssh_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.ssh_host_edit = QLineEdit()
        self.ssh_port_spin = QSpinBox()
        self.ssh_port_spin.setRange(1, 65535)
        self.ssh_port_spin.setValue(22)
        self.ssh_user_edit = QLineEdit()
        self.ssh_pass_edit = QLineEdit()
        self.ssh_pass_edit.setEchoMode(QLineEdit.EchoMode.Password)

        self.ssh_pass_toggle = QPushButton()
        self.ssh_pass_toggle.setCheckable(True)
        self.ssh_pass_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.ssh_pass_toggle.setFlat(True)
        self.ssh_pass_toggle.setFixedWidth(28)
        self._set_ssh_pass_visible(False)
        self.ssh_pass_toggle.toggled.connect(self._set_ssh_pass_visible)

        ssh_pass_row = QWidget()
        ssh_pass_layout = QHBoxLayout(ssh_pass_row)
        ssh_pass_layout.setContentsMargins(0, 0, 0, 0)
        ssh_pass_layout.addWidget(self.ssh_pass_edit)
        ssh_pass_layout.addWidget(self.ssh_pass_toggle)

        ssh_form.addRow("SSH хост:", self.ssh_host_edit)
        ssh_form.addRow("SSH порт:", self.ssh_port_spin)
        ssh_form.addRow("SSH пользователь:", self.ssh_user_edit)
        ssh_form.addRow("SSH пароль:", ssh_pass_row)

        ssh_layout.addLayout(ssh_form)

        # Add rows to form
        form.addRow("Название:", self.name_edit)
        form.addRow("Тип подключения:", self.connection_type_combo)
        form.addRow("Тип БД:", self.db_type_combo)
        form.addRow("Хост:", self.host_edit)
        form.addRow("Порт:", self.port_spin)
        form.addRow("База данных:", self.database_edit)
        form.addRow("Пользователь:", self.username_edit)
        form.addRow("Пароль:", password_row)

        form.addRow(QLabel())  # Spacer
        form.addRow("SSH туннель:", self.ssh_group)

        # Initially hide SSH group
        self.ssh_group.setVisible(False)

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

    def _on_connection_type_changed(self) -> None:
        """Show/hide SSH tunnel fields based on connection type."""
        conn_type = self.connection_type_combo.currentData()
        self.ssh_group.setVisible(conn_type == "ssh_tunnel")

    def _set_password_visible(self, visible: bool) -> None:
        """Toggle password echo mode and the eye button appearance."""
        self.password_edit.setEchoMode(
            QLineEdit.EchoMode.Normal if visible else QLineEdit.EchoMode.Password
        )
        self._set_toggle_icon(self.password_toggle, visible)

    def _set_ssh_pass_visible(self, visible: bool) -> None:
        """Toggle SSH password echo mode and the eye button appearance."""
        self.ssh_pass_edit.setEchoMode(
            QLineEdit.EchoMode.Normal if visible else QLineEdit.EchoMode.Password
        )
        self._set_toggle_icon(self.ssh_pass_toggle, visible)

    def _set_toggle_icon(self, button: QPushButton, visible: bool) -> None:
        """Set eye icon/text on a toggle button."""
        name = "view-visible" if visible else "view-hidden"
        icon = QIcon.fromTheme(name)
        if icon.isNull():
            button.setText("🙈" if visible else "👁")
        else:
            button.setIcon(icon)
            button.setText("")
        button.setToolTip("Скрыть пароль" if visible else "Показать пароль")

    def _load_existing(self, name: str) -> None:
        try:
            cfg = self.store.load_by_name(name)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Ошибка", f"Не удалось загрузить подключение: {e}")
            return
        self.name_edit.setText(name)
        self.db_type_combo.setCurrentText(cfg.type)
        self.host_edit.setText(cfg.host)
        self.port_spin.setValue(cfg.port)
        self.database_edit.setText(cfg.database)
        self.username_edit.setText(cfg.username)
        self.password_edit.setText(cfg.password)

        # Load connection type and SSH tunnel settings
        self.connection_type_combo.setCurrentIndex(
            self.connection_type_combo.findData(cfg.connection_type.value)
        )
        if cfg.ssh_tunnel is not None:
            self.ssh_host_edit.setText(cfg.ssh_tunnel.ssh_host)
            self.ssh_port_spin.setValue(cfg.ssh_tunnel.ssh_port)
            self.ssh_user_edit.setText(cfg.ssh_tunnel.ssh_user)
            # Mask SSH password — store original and show ***
            self._original_ssh_pass = cfg.ssh_tunnel.ssh_pass
            self.ssh_pass_edit.setText("***")

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

        conn_type = ConnectionType(self.connection_type_combo.currentData())

        # Validate SSH tunnel fields if needed
        ssh_tunnel = None
        if conn_type == ConnectionType.SSH_TUNNEL:
            if not self.ssh_host_edit.text().strip():
                QMessageBox.warning(self, "Проверка", "Введите SSH хост.")
                return None
            if not self.ssh_user_edit.text().strip():
                QMessageBox.warning(self, "Проверка", "Введите SSH пользователя.")
                return None
            # If SSH pass shows ***, keep the original (masked on load)
            ssh_pass = self.ssh_pass_edit.text()
            if ssh_pass == "***":
                ssh_pass = self._original_ssh_pass or ""
            ssh_tunnel = SSH_TunnelConfig(
                ssh_host=self.ssh_host_edit.text().strip(),
                ssh_port=self.ssh_port_spin.value(),
                ssh_user=self.ssh_user_edit.text().strip(),
                ssh_pass=ssh_pass,
            )

        return ConnectionConfig(
            name=self.name_edit.text().strip(),
            type=self.db_type_combo.currentText(),
            host=self.host_edit.text().strip(),
            port=self.port_spin.value(),
            database=self.database_edit.text().strip(),
            username=self.username_edit.text().strip(),
            password=self.password_edit.text(),
            connection_type=conn_type,
            ssh_tunnel=ssh_tunnel,
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
            if cfg.connection_type == ConnectionType.SSH_TUNNEL:
                host_desc = f"127.0.0.1:<tunnel>/{cfg.database} (via {cfg.ssh_tunnel.ssh_host})"
            return True, f"Подключено к {host_desc}"
        except DatabaseError as e:
            return False, str(e)
        finally:
            adapter.disconnect()

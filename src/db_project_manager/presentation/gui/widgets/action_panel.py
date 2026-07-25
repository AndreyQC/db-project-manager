"""Action panel widget: dropdown of actions + configure/run/copy-CLI container.

One container for all actions (see Phase 7 vision): a QComboBox filled from the
action registry, a read-only summary of the current settings, «Настроить…» /
«Выполнить» buttons, and a read-only CLI line with a «Копировать» button.
Execution itself is delegated to MainWindow via the execute_requested signal.
"""

from __future__ import annotations

from typing import Callable

from pydantic import BaseModel
from PySide6.QtCore import Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QGroupBox,
    QHBoxLayout,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from db_project_manager.infrastructure.config.app_config import CFG
from db_project_manager.infrastructure.config.connection_store import ConnectionStore
from db_project_manager.infrastructure.config.gui_settings import GuiSettingsStore

from db_project_manager.presentation.gui.actions.registry import (
    ACTIONS,
    ActionSpec,
    get_action,
)

# Human-readable labels for settings summary (ключ: значение).
FIELD_LABELS = {
    "connection": "подключение",
    "output_dir": "каталог вывода",
    "codebase_dir": "каталог кодовой базы",
    "prefix": "префикс имени БД",
    "keep_db": "оставить временную БД",
    "continue_on_error": "продолжать при ошибках",
    "format": "формат экспорта",
    "validate_graph": "проверить граф",
}

# Per-action label overrides (same field name, different meaning).
ACTION_FIELD_LABELS = {
    "graph_prepare": {"output_dir": "каталог для файла экспорта"},
}


class ActionPanelWidget(QGroupBox):
    """Container with action dropdown, settings summary and run/copy buttons."""

    execute_requested = Signal(str, object)  # action_id, settings model instance
    status_message = Signal(str)

    def __init__(
        self,
        store: ConnectionStore,
        settings_store: GuiSettingsStore,
        cfg: CFG,
        get_selected_connection: Callable[[], str | None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__("Действие", parent)
        self.store = store
        self.settings_store = settings_store
        self.cfg = cfg
        self.get_selected_connection = get_selected_connection

        layout = QVBoxLayout(self)

        self.action_combo = QComboBox()
        for spec in ACTIONS:
            self.action_combo.addItem(spec.title, userData=spec.action_id)
        self.action_combo.currentIndexChanged.connect(self._on_action_changed)
        layout.addWidget(self.action_combo)

        self.summary_edit = QPlainTextEdit()
        self.summary_edit.setReadOnly(True)
        self.summary_edit.setMaximumBlockCount(8)
        self.summary_edit.setFixedHeight(90)
        layout.addWidget(self.summary_edit)

        buttons_row = QHBoxLayout()
        self.configure_btn = QPushButton("Настроить…")
        self.configure_btn.clicked.connect(self._on_configure)
        buttons_row.addWidget(self.configure_btn)
        self.run_btn = QPushButton("Выполнить")
        self.run_btn.clicked.connect(self._on_run)
        buttons_row.addWidget(self.run_btn)
        buttons_row.addStretch(1)
        layout.addLayout(buttons_row)

        cli_row = QHBoxLayout()
        self.cli_edit = QLineEdit()
        self.cli_edit.setReadOnly(True)
        self.cli_edit.setPlaceholderText("CLI-команда появится после настройки действия")
        cli_row.addWidget(self.cli_edit, stretch=1)
        self.copy_btn = QPushButton("Копировать")
        self.copy_btn.clicked.connect(self._on_copy_cli)
        cli_row.addWidget(self.copy_btn)
        layout.addLayout(cli_row)

        # Restore the last used action (falls back to the first one).
        last = self.settings_store.get_last_action()
        if last:
            idx = self.action_combo.findData(last)
            if idx >= 0:
                self.action_combo.setCurrentIndex(idx)
        self._refresh()

    # --- current action/settings ---

    def current_spec(self) -> ActionSpec:
        return get_action(self.action_combo.currentData())

    def current_settings(self) -> BaseModel:
        """Saved settings of the current action with defaults applied."""
        spec = self.current_spec()
        saved = self.settings_store.get_action_settings(spec.action_id)
        settings = spec.settings_model.model_validate(saved)
        return self._apply_defaults(spec, settings)

    def _apply_defaults(self, spec: ActionSpec, settings: BaseModel) -> BaseModel:
        """Fill empty REQUIRED fields from the list selection and config.

        Optional fields (e.g. graph export output_dir) are left empty so the
        action's own default (<codebase>/.dbm_graph/) applies.
        """
        updates: dict[str, str] = {}
        for field in spec.required_fields:
            value = getattr(settings, field, None)
            if isinstance(value, str) and not value:
                if field == "connection":
                    selected = self.get_selected_connection()
                    if selected:
                        updates[field] = selected
                elif field.endswith("_dir"):
                    updates[field] = self.cfg.paths.default_output_dir
        return settings.model_copy(update=updates) if updates else settings

    def _missing_required(self, spec: ActionSpec, settings: BaseModel) -> list[str]:
        return [f for f in spec.required_fields if not getattr(settings, f, None)]

    # --- UI refresh ---

    def _refresh(self) -> None:
        spec = self.current_spec()
        settings = self.current_settings()

        lines = []
        labels = {**FIELD_LABELS, **ACTION_FIELD_LABELS.get(spec.action_id, {})}
        for field in type(settings).model_fields:
            value = getattr(settings, field)
            label = labels.get(field, field)
            if isinstance(value, bool):
                display = "да" if value else "нет"
            else:
                display = str(value) if value else "—"
            lines.append(f"{label}: {display}")
        self.summary_edit.setPlainText("\n".join(lines))

        if self._missing_required(spec, settings):
            self.cli_edit.setText("")
        else:
            self.cli_edit.setText(spec.build_cli(settings, self.store))

    # --- slots ---

    def _on_action_changed(self, index: int) -> None:
        del index
        self._refresh()

    def _on_configure(self) -> bool:
        """Open the settings dialog. Returns True if settings were saved (OK)."""
        spec = self.current_spec()
        dialog = spec.make_dialog(self.store, self.current_settings(), self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return False
        settings = dialog.settings()
        self.settings_store.save_action_settings(
            spec.action_id, settings.model_dump(mode="json")
        )
        self._refresh()
        return True

    def _on_run(self) -> None:
        spec = self.current_spec()
        settings = self.current_settings()
        if self._missing_required(spec, settings):
            # U7: no valid defaults -> force configuration first.
            if not self._on_configure():
                return
            settings = self.current_settings()
            if self._missing_required(spec, settings):
                self.status_message.emit("Действие не настроено: заполните обязательные поля.")
                return
        self.execute_requested.emit(spec.action_id, settings)

    def _on_copy_cli(self) -> None:
        cli = self.cli_edit.text()
        if not cli:
            self.status_message.emit("CLI-команда пуста: сначала настройте действие.")
            return
        QGuiApplication.clipboard().setText(cli)
        self.status_message.emit("CLI-команда скопирована в буфер обмена.")

    # --- external control (MainWindow) ---

    def set_running(self, running: bool) -> None:
        self.run_btn.setEnabled(not running)
        self.configure_btn.setEnabled(not running)
        self.action_combo.setEnabled(not running)

    def mark_executed(self) -> None:
        """Persist current settings after a successful run (vision §7)."""
        spec = self.current_spec()
        settings = self.current_settings()
        self.settings_store.save_action_settings(
            spec.action_id, settings.model_dump(mode="json")
        )
        self.settings_store.set_last_action(spec.action_id)

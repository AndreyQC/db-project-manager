"""Per-action "Настроить…" dialogs for the GUI action panel.

One dialog per action; each edits the action's settings model. Prefill with
saved/default values happens via set_settings(); the panel persists the result
on OK (gui_settings.json).
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QWidget,
)

from db_project_manager.infrastructure.config.connection_store import ConnectionStore

from db_project_manager.presentation.gui.actions.models import (
    EXPORT_FORMATS,
    FORMAT_NONE,
    DeployValidateSettings,
    GraphPrepareSettings,
    ReverseEngineerSettings,
)

FORMAT_LABELS = {
    "graphml": "graphml (Gephi)",
    "json": "json",
    "dot": "dot (Graphviz)",
    FORMAT_NONE: "только build (без экспорта)",
}


class BaseActionDialog(QDialog):
    """Common skeleton: form + OK/Cancel, settings roundtrip."""

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self._form = QFormLayout(self)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self._form.addRow(buttons)

    # --- helpers ---

    def _dir_row(self, value: str, caption: str) -> QLineEdit:
        """A read-only-ish path edit with a «Выбрать…» browse button."""
        edit = QLineEdit(value)
        row = QHBoxLayout()
        row.addWidget(edit, stretch=1)
        browse = QPushButton("Выбрать…")
        browse.clicked.connect(lambda: self._browse_dir(edit, caption))
        row.addWidget(browse)
        self._form.addRow(caption, row)
        return edit

    def _browse_dir(self, edit: QLineEdit, caption: str) -> None:
        start = edit.text().strip() or str(Path.home())
        directory = QFileDialog.getExistingDirectory(self, caption, start)
        if directory:
            edit.setText(directory)

    def _connections_combo(self, store: ConnectionStore, current: str) -> QComboBox:
        combo = QComboBox()
        combo.setEditable(False)
        combo.addItems(store.list_names())
        if current:
            idx = combo.findText(current)
            if idx >= 0:
                combo.setCurrentIndex(idx)
        return combo

    # --- interface ---

    def settings(self):  # noqa: ANN201 — per-subclass settings model
        raise NotImplementedError


class ReverseEngineerDialog(BaseActionDialog):
    """Settings for 'Создать проект базы по подключению PG'."""

    def __init__(
        self,
        store: ConnectionStore,
        settings: ReverseEngineerSettings,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__("Создать проект базы по подключению PG — настройки", parent)
        self._connection = self._connections_combo(store, settings.connection)
        self._form.insertRow(0, "Подключение:", self._connection)
        self._output_dir = self._dir_row(settings.output_dir, "Каталог вывода:")

    def settings(self) -> ReverseEngineerSettings:
        return ReverseEngineerSettings(
            connection=self._connection.currentText(),
            output_dir=self._output_dir.text().strip(),
        )


class DeployValidateDialog(BaseActionDialog):
    """Settings for 'Выполнить тестовый деплой из проекта базы'."""

    def __init__(
        self,
        store: ConnectionStore,
        settings: DeployValidateSettings,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__("Тестовый деплой — настройки", parent)
        self._codebase_dir = self._dir_row(settings.codebase_dir, "Каталог кодовой базы:")
        self._connection = self._connections_combo(store, settings.connection)
        self._form.addRow("Подключение (куда):", self._connection)
        self._prefix = QLineEdit(settings.prefix)
        self._prefix.setPlaceholderText("по умолчанию: имя каталога кодовой базы")
        self._form.addRow("Префикс имени БД:", self._prefix)
        self._keep_db = QCheckBox("Оставить временную БД после деплоя (для отладки)")
        self._keep_db.setChecked(settings.keep_db)
        self._form.addRow(self._keep_db)
        self._continue_on_error = QCheckBox(
            "Продолжать при ошибках в views/functions/procedures"
        )
        self._continue_on_error.setChecked(settings.continue_on_error)
        self._form.addRow(self._continue_on_error)

    def settings(self) -> DeployValidateSettings:
        return DeployValidateSettings(
            codebase_dir=self._codebase_dir.text().strip(),
            connection=self._connection.currentText(),
            prefix=self._prefix.text().strip(),
            keep_db=self._keep_db.isChecked(),
            continue_on_error=self._continue_on_error.isChecked(),
        )


class GraphPrepareDialog(BaseActionDialog):
    """Settings for 'Подготовить граф для просмотра в Gephi'."""

    def __init__(
        self,
        store: ConnectionStore,
        settings: GraphPrepareSettings,
        parent: QWidget | None = None,
    ) -> None:
        del store  # graph actions do not use a connection
        super().__init__("Подготовить граф — настройки", parent)
        self._codebase_dir = self._dir_row(settings.codebase_dir, "Каталог кодовой базы:")
        self._format = QComboBox()
        for fmt in EXPORT_FORMATS:
            self._format.addItem(FORMAT_LABELS[fmt], userData=fmt)
        idx = self._format.findData(settings.format)
        if idx >= 0:
            self._format.setCurrentIndex(idx)
        self._form.addRow("Формат экспорта:", self._format)
        self._validate = QCheckBox("Проверить граф (циклы, висячие ссылки)")
        self._validate.setChecked(settings.validate_graph)
        self._form.addRow(self._validate)

    def settings(self) -> GraphPrepareSettings:
        return GraphPrepareSettings(
            codebase_dir=self._codebase_dir.text().strip(),
            format=self._format.currentData(),
            validate_graph=self._validate.isChecked(),
        )

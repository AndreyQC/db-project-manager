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
    QLabel,
    QLineEdit,
    QPushButton,
    QWidget,
)

from db_project_manager.infrastructure.config.connection_store import ConnectionStore

from db_project_manager.presentation.gui.actions.models import (
    EXPORT_FORMATS,
    FORMAT_NONE,
    CompareSettings,
    DeployAnalyzeSettings,
    DeployApplySettings,
    DeployValidateSettings,
    GraphPrepareSettings,
    ReverseEngineerSettings,
    YamlApplySettings,
    YamlGenerateSettings,
)

FORMAT_LABELS = {
    "graphml": "graphml (Gephi)",
    "json": "json",
    "dot": "dot (Graphviz)",
    FORMAT_NONE: "только build (без экспорта)",
}

#: Placeholder for the "no connection selected" combo entry (compare action),
#: where a side can be a directory instead. Read back as "" in settings().
CONNECTION_EMPTY_LABEL = "(каталог вместо подключения)"


class BaseActionDialog(QDialog):
    """Common skeleton: form + OK/Cancel, settings roundtrip.

    Subclasses build their fields and then MUST call ``_add_buttons()`` last,
    so the button box stays at the bottom of the dialog.
    """

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self._form = QFormLayout(self)

    def _add_buttons(self) -> None:
        """Append the OK/Cancel button box as the last row of the form."""
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self._form.addRow(buttons)

    # --- helpers ---

    def _dir_row(self, value: str, caption: str, placeholder: str = "") -> QLineEdit:
        """A read-only-ish path edit with a «Выбрать…» browse button."""
        edit = QLineEdit(value)
        if placeholder:
            edit.setPlaceholderText(placeholder)
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

    def _connections_combo(
        self, store: ConnectionStore, current: str, *, allow_empty: bool = False
    ) -> QComboBox:
        """Connection dropdown populated from ``store.list_names()``.

        When ``allow_empty`` is True (compare action), a leading placeholder entry
        (:data:`CONNECTION_EMPTY_LABEL`) lets the user pick "no connection — I'll use
        a directory instead", read back as "" in settings(). This avoids the XOR
        violation that otherwise occurs because the combo defaults to index 0 (the
        first connection) whenever the user fills the directory field.
        """
        combo = QComboBox()
        combo.setEditable(False)
        if allow_empty:
            combo.addItem(CONNECTION_EMPTY_LABEL, userData="")
        for name in store.list_names():
            combo.addItem(name, userData=name)
        if current:
            idx = combo.findData(current)
            if idx >= 0:
                combo.setCurrentIndex(idx)
        elif allow_empty:
            combo.setCurrentIndex(0)  # the placeholder
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
        self._form.addRow("Подключение:", self._connection)
        self._output_dir = self._dir_row(settings.output_dir, "Каталог вывода:")
        self._add_buttons()

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
        self._add_buttons()

    def settings(self) -> DeployValidateSettings:
        return DeployValidateSettings(
            codebase_dir=self._codebase_dir.text().strip(),
            connection=self._connection.currentText(),
            prefix=self._prefix.text().strip(),
            keep_db=self._keep_db.isChecked(),
            continue_on_error=self._continue_on_error.isChecked(),
        )


class DeployAnalyzeDialog(BaseActionDialog):
    """Settings for 'Safety gate: проанализировать деплой' (Phase 11, SG-7)."""

    def __init__(
        self,
        store: ConnectionStore,
        settings: DeployAnalyzeSettings,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__("Safety gate (deploy analyze) — настройки", parent)
        self._codebase_dir = self._dir_row(settings.codebase_dir, "Каталог кодовой базы:")
        self._target_connection = self._connections_combo(store, settings.target_connection)
        self._form.addRow("Целевая БД (существует, с данными):", self._target_connection)
        self._output_dir = self._dir_row(
            settings.output_dir,
            "Каталог для отчётов:",
            placeholder="safety_gate_report.md / .json / diff_report.json",
        )
        self._add_buttons()

    def settings(self) -> DeployAnalyzeSettings:
        return DeployAnalyzeSettings(
            codebase_dir=self._codebase_dir.text().strip(),
            target_connection=self._target_connection.currentText(),
            output_dir=self._output_dir.text().strip(),
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
        self._output_dir = self._dir_row(
            settings.output_dir,
            "Каталог для файла экспорта:",
            placeholder="по умолчанию: <кодовая база>/.dbm_graph",
        )
        self._add_buttons()

    def settings(self) -> GraphPrepareSettings:
        return GraphPrepareSettings(
            codebase_dir=self._codebase_dir.text().strip(),
            format=self._format.currentData(),
            validate_graph=self._validate.isChecked(),
            output_dir=self._output_dir.text().strip(),
        )


class CompareDialog(BaseActionDialog):
    """Settings for 'Сравнить состояния (БД или каталог reverse-engineer)'.

    Two sides (source/target); each side offers a connection combo AND a directory
    row — exactly one must be filled per side (XOR enforced at SideSpec build time).
    """

    def __init__(
        self,
        store: ConnectionStore,
        settings: CompareSettings,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__("Сравнение состояний — настройки", parent)

        # allow_empty=True: a side may be a directory instead of a connection.
        # The leading placeholder (CONNECTION_EMPTY_LABEL) lets the user pick
        # "no connection" so filling the directory field does not violate XOR.
        self._source_connection = self._connections_combo(
            store, settings.source_connection, allow_empty=True
        )
        self._form.addRow("Source: подключение (БД):", self._source_connection)
        self._source_dir = self._dir_row(
            settings.source_dir,
            "Source: каталог reverse-engineer:",
            placeholder="укажите ИЛИ подключение, ИЛИ каталог",
        )

        self._target_connection = self._connections_combo(
            store, settings.target_connection, allow_empty=True
        )
        self._form.addRow("Target: подключение (БД):", self._target_connection)
        self._target_dir = self._dir_row(
            settings.target_dir,
            "Target: каталог reverse-engineer:",
            placeholder="укажите ИЛИ подключение, ИЛИ каталог",
        )

        self._output_dir = self._dir_row(settings.output_dir, "Каталог для отчётов:")
        self._keep_model_dir = QCheckBox("Сохранить временный каталог reverse-engineer (для отладки)")
        self._keep_model_dir.setChecked(settings.keep_model_dir)
        self._form.addRow(self._keep_model_dir)

        self._add_buttons()  # LESSONS §43 — last row of the form

    def settings(self) -> CompareSettings:
        return CompareSettings(
            # currentData() returns "" for the placeholder entry, the connection
            # name otherwise — so an unset connection is read back as "".
            source_connection=str(self._source_connection.currentData() or ""),
            source_dir=self._source_dir.text().strip(),
            target_connection=str(self._target_connection.currentData() or ""),
            target_dir=self._target_dir.text().strip(),
            output_dir=self._output_dir.text().strip(),
            keep_model_dir=self._keep_model_dir.isChecked(),
        )


class YamlGenerateDialog(BaseActionDialog):
    """Settings for 'db-pm yaml generate' (Phase 13)."""

    def __init__(
        self,
        store: ConnectionStore,
        settings: YamlGenerateSettings,
        parent: QWidget | None = None,
    ) -> None:
        del store  # yaml generate does not use connections
        super().__init__("YAML generate — настройки", parent)
        self._source_dir = self._dir_row(
            settings.source_dir,
            "Каталог с SQL-файлами:",
            placeholder="например C:\\YandexDisk\\...\\cis_zup",
        )
        self._db_type = QComboBox()
        self._db_type.addItem("Greenplum", userData="greenplum")
        self._db_type.addItem("PostgreSQL", userData="postgres")
        idx = self._db_type.findData(settings.db_type)
        if idx >= 0:
            self._db_type.setCurrentIndex(idx)
        self._form.addRow("Тип БД-источника:", self._db_type)
        self._output_file = self._dir_row(
            settings.output_file,
            "Выходной YAML-файл:",
            placeholder="например output.yaml",
        )
        self._source_version = QLineEdit(settings.source_version)
        self._source_version.setPlaceholderText("необязательно, например 2026.08.27.01")
        self._form.addRow("Версия источника (calver):", self._source_version)
        self._add_buttons()

    def settings(self) -> YamlGenerateSettings:
        return YamlGenerateSettings(
            source_dir=self._source_dir.text().strip(),
            db_type=self._db_type.currentData(),
            output_file=self._output_file.text().strip(),
            source_version=self._source_version.text().strip(),
        )


class YamlApplyDialog(BaseActionDialog):
    """Settings for 'db-pm yaml apply' (Phase 13)."""

    def __init__(
        self,
        store: ConnectionStore,
        settings: YamlApplySettings,
        parent: QWidget | None = None,
    ) -> None:
        del store  # yaml apply does not use connections
        super().__init__("YAML apply — настройки", parent)
        self._yaml_file = self._dir_row(
            settings.yaml_file,
            "YAML-файл:",
            placeholder="выберите .yaml файл",
        )
        self._target_db_type = QComboBox()
        self._target_db_type.addItem("PostgreSQL", userData="postgres")
        self._target_db_type.addItem("Greenplum", userData="greenplum")
        idx = self._target_db_type.findData(settings.target_db_type)
        if idx >= 0:
            self._target_db_type.setCurrentIndex(idx)
        self._form.addRow("Целевой тип БД:", self._target_db_type)
        self._output_dir = self._dir_row(
            settings.output_dir,
            "Каталог кодовой базы (output):",
            placeholder="например C:\\Projects\\my_codebase",
        )
        self._add_buttons()

    def settings(self) -> YamlApplySettings:
        return YamlApplySettings(
            yaml_file=self._yaml_file.text().strip(),
            target_db_type=self._target_db_type.currentData(),
            output_dir=self._output_dir.text().strip(),
        )


class DeployPlanDialog(BaseActionDialog):
    """Settings for 'Сформировать план деплоя на существующую БД' (Phase 15).

    Dry-run: writes ``plan.json`` / ``plan.md`` / ``delta/NNN_*.sql`` artifacts,
    but does NOT mutate the target. ``--include-drops`` is the only optional flag.
    """

    def __init__(
        self,
        store: ConnectionStore,
        settings: DeployApplySettings,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__("Deploy plan — настройки", parent)
        self._codebase_dir = self._dir_row(settings.codebase_dir, "Каталог кодовой базы:")
        self._target_connection = self._connections_combo(store, settings.target_connection)
        self._form.addRow("Целевая БД (существует):", self._target_connection)
        self._output_dir = self._dir_row(
            settings.output_dir,
            "Каталог для артефактов:",
            placeholder="plan.json / plan.md / delta/NNN_*.sql",
        )
        self._include_drops = QCheckBox(
            "Включить DROP-артефакты для удалённых объектов (пустые/не-табличные)"
        )
        self._include_drops.setChecked(settings.include_drops)
        self._form.addRow(self._include_drops)
        self._add_buttons()  # LESSONS §43 — last row of the form

    def settings(self) -> DeployApplySettings:
        return DeployApplySettings(
            codebase_dir=self._codebase_dir.text().strip(),
            target_connection=self._target_connection.currentText(),
            output_dir=self._output_dir.text().strip(),
            include_drops=self._include_drops.isChecked(),
        )


class DeployApplyDialog(BaseActionDialog):
    """Settings for 'Применить деплой к существующей БД' (Phase 15, PRE-2).

    This dialog MUTATES an existing database. Preflight-warning:
    - Red, bold label at the top of the form: «⚠ Изменяет существующую БД.
      Репетиция обязательна (кроме CI).»
    - Confirmation checkbox «Я понимаю последствия и хочу применить»;
      the OK button is disabled until checked (LESSONS §43 + preflight pattern).
    ``confirm_understands_risk`` is stored back into the settings but the
    CLI builder ignores it (GUI-side gate, not part of the CLI contract).
    """

    def __init__(
        self,
        store: ConnectionStore,
        settings: DeployApplySettings,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__("Deploy apply — настройки ⚠", parent)
        # Preflight warning — first row of the form, red and bold.
        warning = QLabel(
            "⚠ Изменяет существующую БД. Репетиция обязательна (кроме CI)."
        )
        warning.setStyleSheet("color: red; font-weight: bold")
        warning.setWordWrap(True)
        self._form.addRow(warning)

        self._codebase_dir = self._dir_row(settings.codebase_dir, "Каталог кодовой базы:")
        self._target_connection = self._connections_combo(store, settings.target_connection)
        self._form.addRow("Целевая БД (существует):", self._target_connection)
        self._output_dir = self._dir_row(
            settings.output_dir,
            "Каталог для артефактов:",
            placeholder="plan.json / plan.md / delta/ / rehearsal/",
        )
        self._include_drops = QCheckBox(
            "Включить DROP-артефакты для удалённых объектов (пустые/не-табличные)"
        )
        self._include_drops.setChecked(settings.include_drops)
        self._form.addRow(self._include_drops)
        self._no_rehearsal = QCheckBox(
            "Пропустить репетицию (только CI / throwaway-таргеты)"
        )
        self._no_rehearsal.setChecked(settings.no_rehearsal)
        self._form.addRow(self._no_rehearsal)
        self._keep_rehearsal_db = QCheckBox(
            "Оставить rehearsal-БД после прогона (для отладки)"
        )
        self._keep_rehearsal_db.setChecked(settings.keep_rehearsal_db)
        self._form.addRow(self._keep_rehearsal_db)

        # Confirmation gate — must be checked to enable OK.
        self._confirm = QCheckBox("Я понимаю последствия и хочу применить")
        self._confirm.setChecked(settings.confirm_understands_risk)
        self._form.addRow(self._confirm)

        # _add_buttons() must come last (LESSONS §43) — but the OK button needs
        # to be wired to the confirmation checkbox BEFORE it is added.
        # We therefore reach into the button box after _add_buttons() returns.
        self._add_buttons()
        ok_button = self._button_box.button(QDialogButtonBox.StandardButton.Ok)
        if ok_button is not None:
            ok_button.setEnabled(self._confirm.isChecked())
            self._confirm.stateChanged.connect(
                lambda _state: ok_button.setEnabled(self._confirm.isChecked())
            )

    def _add_buttons(self) -> None:
        """Override base _add_buttons() to keep a reference to the box.

        The confirmation gate (see class docstring) needs to access the OK button
        after construction, so we save it on ``self._button_box``.
        """
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self._form.addRow(buttons)
        self._button_box = buttons

    def settings(self) -> DeployApplySettings:
        return DeployApplySettings(
            codebase_dir=self._codebase_dir.text().strip(),
            target_connection=self._target_connection.currentText(),
            output_dir=self._output_dir.text().strip(),
            include_drops=self._include_drops.isChecked(),
            no_rehearsal=self._no_rehearsal.isChecked(),
            keep_rehearsal_db=self._keep_rehearsal_db.isChecked(),
            confirm_understands_risk=self._confirm.isChecked(),
        )

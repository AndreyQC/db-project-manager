"""Plan Viewer window — browse a ``plan.json`` interactively (Phase 15, PRE-3).

A standalone ``QMainWindow`` that opens an existing ``plan.json`` (produced by
``db-pm deploy plan``) and presents it as a tree of operations grouped by
type/schema, color-coded by classification (safe / needs-pre / blocked), with
a detail panel and a DDL view of the per-operation ``script_file`` artifact.

Design (see ``_tasks_/phase_15/Phase_15_vision_final.md`` §4):
    - Reads a single self-describing ``plan.json`` (the ``DeltaPlan`` model).
    - Separate window (not a dock; not an action dialog — those close on OK).
      Mirrors :class:`DeltaViewerWindow` (Phase 14) in lifecycle.
    - Worker for parsing the JSON off the UI thread (large schemas). Signals
      connected to bound methods only; the worker is held by a strong ref until
      ``finished`` (LESSONS §42).
    - Prefill state (target_connection / codebase_dir / output_dir) is captured
      from ``load_from_path(...)`` so the toolbar «Применить» action can launch
      a :class:`~db_project_manager.presentation.gui.actions.dialogs.DeployApplyDialog`
      with the right defaults (PRE-3). The ``ConnectionStore`` is passed in via
      the constructor; when ``None`` the apply button is disabled.

Not done (Phase 15 §7):
    - Edge diff tab (Phase 14) — not relevant for plans.
    - Markdown export — ``plan.md`` is already produced by ``db-pm deploy plan``
      and lives next to ``plan.json``.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QThreadPool
from PySide6.QtGui import QAction, QColor
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QFormLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QSplitter,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from db_project_manager.domain.delta import DeltaPlan, OperationClass, PlannedOperation
from db_project_manager.infrastructure.config.connection_store import ConnectionStore
from db_project_manager.presentation.gui.widgets.sql_highlighter import SqlHighlighter
from db_project_manager.presentation.gui.widgets.workers import LoadPlanReportWorker

#: Display order in the filter row and summary.
_CLASS_ORDER = (
    OperationClass.SAFE,
    OperationClass.NEEDS_PRE,
    OperationClass.BLOCKED,
)

#: Colors per classification (Phase 15, PRE-1). Dark-theme-friendly hex.
_CLASSIFICATION_COLORS: dict[OperationClass, str] = {
    OperationClass.SAFE: "#2EA043",       # green
    OperationClass.NEEDS_PRE: "#D29922",  # yellow/amber
    OperationClass.BLOCKED: "#F85149",    # red
}

#: Qt item-data role carrying the OperationClass of a tree leaf (.value, str).
_CLASS_ROLE = Qt.ItemDataRole.UserRole
#: Qt item-data role carrying the PlannedOperation on leaf nodes.
_OPERATION_ROLE = Qt.ItemDataRole.UserRole + 1
#: Qt item-data role marking a node as an operation leaf (vs group nodes).
_IS_OPERATION_ROLE = Qt.ItemDataRole.UserRole + 2


class PlanViewerWindow(QMainWindow):
    """Browse a ``plan.json`` as a tree + detail + DDL tabs (Phase 15, PRE-1).

    Constructor parameters:
        connection_store: the GUI's :class:`ConnectionStore` — required for the
            «Применить» toolbar action to open a ``DeployApplyDialog``. If
            ``None``, the button is disabled.
        target_connection / codebase_dir / output_dir: optional prefill state
            captured by the caller (main window) so opening a plan from an
            analyze/plan dialog remembers where it came from. They can also be
            overwritten by ``load_from_path(...)``.
    """

    def __init__(
        self,
        *,
        connection_store: ConnectionStore | None = None,
        target_connection: str = "",
        codebase_dir: str = "",
        output_dir: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Plan Viewer")
        self.setMinimumSize(1000, 700)

        self.thread_pool = QThreadPool.globalInstance()
        # Strong refs to the load worker (LESSONS §42).
        self._active_workers: dict = {}
        self._plan: DeltaPlan | None = None
        self._plan_path: Path | None = None
        self._connection_store = connection_store
        # Prefill state for the «Применить» toolbar action (PRE-3).
        self._target_connection = target_connection
        self._codebase_dir = codebase_dir
        self._output_dir = output_dir
        # Filter state — kept as str (Enum.value) because PySide6 does not
        # preserve enum identity across QTreeWidgetItem.setData round-trip.
        self._class_filter: dict[str, bool] = {c.value: True for c in _CLASS_ORDER}

        self._init_ui()

    # --- UI construction ---

    def _init_ui(self) -> None:
        self._build_toolbar()

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        self._summary_label = QLabel("(план не загружен)")
        self._summary_label.setStyleSheet("padding: 4px;")
        root.addWidget(self._summary_label)

        # Filter row (one QAction toggling each classification).
        from PySide6.QtWidgets import QToolBar

        filter_tb = QToolBar("Filters")
        filter_tb.setMovable(False)
        root.addWidget(filter_tb)
        self._class_actions: dict[OperationClass, QAction] = {}
        for cls in _CLASS_ORDER:
            act = QAction(f"{cls.value}", self)
            act.setCheckable(True)
            act.setChecked(True)
            act.triggered.connect(self._on_filter_changed)
            act.setText(self._format_class_label(cls))
            filter_tb.addAction(act)
            self._class_actions[cls] = act

        # Splitter: tree on the left, tabs on the right.
        self._splitter = QSplitter(Qt.Orientation.Horizontal)
        root.addWidget(self._splitter, stretch=1)

        self._tree = QTreeWidget()
        self._tree.setHeaderLabels(["Объект", "Класс"])
        self._tree.itemClicked.connect(self._on_tree_clicked)
        self._splitter.addWidget(self._tree)

        right = QTabWidget()
        self._splitter.addWidget(right)
        self._splitter.setStretchFactor(0, 2)
        self._splitter.setStretchFactor(1, 3)

        # --- Детали ---
        self._details_widget = QWidget()
        self._detail_form = QFormLayout(self._details_widget)
        self._detail_fields: dict[str, QLabel] = {}
        for key, label_text in (
            ("object_key", "object_key:"),
            ("object_schema", "object_schema:"),
            ("object_name", "object_name:"),
            ("object_type", "object_type:"),
            ("action", "action:"),
            ("classification", "classification:"),
            ("estimated_rows", "estimated_rows:"),
            ("covered_by", "covered_by:"),
            ("script_file", "script_file:"),
            ("reason", "reason:"),
        ):
            lbl = QLabel("—")
            lbl.setWordWrap(True)
            self._detail_fields[key] = lbl
            self._detail_form.addRow(label_text, lbl)
        right.addTab(self._details_widget, "Детали")

        # --- DDL ---
        self._ddl_view = QPlainTextEdit()
        self._ddl_view.setReadOnly(True)
        font = self._ddl_view.font()
        font.setFamily("Consolas")
        font.setStyleHint(font.StyleHint.Monospace)
        self._ddl_view.setFont(font)
        self._ddl_view.setPlaceholderText("(выберите операцию в дереве — здесь будет DDL из script_file)")
        # SqlHighlighter is set on a fresh document later (when DDL is loaded)
        # — see _show_ddl. Creating it here would highlight the placeholder.
        right.addTab(self._ddl_view, "DDL")

    def _build_toolbar(self) -> None:
        tb = self.addToolBar("Plan")
        tb.setMovable(False)

        open_act = QAction("Открыть JSON…", self)
        open_act.setToolTip("Выбрать plan.json для просмотра")
        open_act.triggered.connect(self._on_open_json)
        tb.addAction(open_act)

        # PRE-3: apply button. Disabled until we know the target_connection
        # (set by load_from_path or by the caller).
        self._apply_act = QAction("Применить…", self)
        self._apply_act.setToolTip(
            "Открыть DeployApplyDialog с предзаполненными полями (нужны target_connection, codebase_dir, output_dir)."
        )
        self._apply_act.triggered.connect(self._on_apply)
        self._apply_act.setEnabled(bool(self._target_connection and self._connection_store))
        tb.addAction(self._apply_act)

    def _format_class_label(self, cls: OperationClass) -> str:
        """Short Russian label for the filter chip."""
        return {
            OperationClass.SAFE: "safe (безопасно)",
            OperationClass.NEEDS_PRE: "needs-pre (pre-скрипт)",
            OperationClass.BLOCKED: "BLOCKED (блок)",
        }[cls]

    # --- loading ---

    def _on_open_json(self) -> None:
        start = str(self._plan_path.parent) if self._plan_path else str(Path.home())
        path_str, _ = QFileDialog.getOpenFileName(
            self,
            "Открыть plan.json",
            start,
            "Plan JSON (*.json);;All files (*)",
        )
        if path_str:
            self.load_from_path(path_str)

    def load_from_path(self, path: str | Path) -> None:
        """Asynchronously load a ``plan.json`` (off the UI thread)."""
        path = Path(path)
        self._plan_path = path
        # Default output_dir = the directory of the plan.json — used for
        # resolving ``script_file`` (e.g. delta/NNN_*.sql) and as a fallback
        # for the «Применить» prefill if the caller did not supply one.
        if not self._output_dir:
            self._output_dir = str(path.parent)
        self._summary_label.setText(f"Загрузка: {path.name} …")

        worker = LoadPlanReportWorker(path)
        worker.signals.status.connect(self._on_load_status)
        worker.signals.error.connect(self._on_load_error)
        worker.signals.finished.connect(self._on_load_finished)
        self._active_workers[worker.signals] = worker
        self.thread_pool.start(worker)

    def show_plan(
        self,
        plan: DeltaPlan,
        *,
        path: str | Path | None = None,
    ) -> None:
        """Populate the tree from an already-parsed plan (synchronous, test-friendly).

        Used by tests and by :meth:`_on_load_finished` after the worker parses the JSON.
        """
        self._plan = plan
        if path is not None:
            self._plan_path = Path(path)
        self._populate_tree(plan)
        self._populate_summary(plan)

    def set_prefill(
        self,
        *,
        target_connection: str = "",
        codebase_dir: str = "",
        output_dir: str = "",
    ) -> None:
        """Update prefill state and re-evaluate the «Применить» button enable."""
        if target_connection:
            self._target_connection = target_connection
        if codebase_dir:
            self._codebase_dir = codebase_dir
        if output_dir:
            self._output_dir = output_dir
        self._apply_act.setEnabled(
            bool(self._target_connection and self._connection_store)
        )

    # --- worker callbacks ---

    def _on_load_status(self, message: str) -> None:
        self._summary_label.setText(message)

    def _on_load_error(self, message: str) -> None:
        self._summary_label.setText(f"Ошибка: {message}")
        QMessageBox.warning(self, "Plan Viewer", message)

    def _on_load_finished(self, plan: object) -> None:
        # Strong ref can be released now (LESSONS §42).
        signals = self.sender()
        self._active_workers.pop(signals, None)
        if plan is None:
            return
        assert isinstance(plan, DeltaPlan)
        self.show_plan(plan, path=self._plan_path)

    # --- tree population ---

    def _populate_tree(self, plan: DeltaPlan) -> None:
        """Build the grouped tree (type -> schema -> object) from plan operations."""
        self._tree.clear()

        # Group by type/schema, preserving operation order from the plan.
        by_type: dict[str, dict[str | None, list[PlannedOperation]]] = {}
        for op in plan.operations:
            by_type.setdefault(op.object_type, {}).setdefault(op.object_schema, []).append(op)

        for object_type in sorted(by_type):
            type_bucket = by_type[object_type]
            total = sum(len(v) for v in type_bucket.values())
            type_node = QTreeWidgetItem([f"{object_type} ({total})", ""])
            f = type_node.font(0)
            f.setBold(True)
            type_node.setFont(0, f)
            self._tree.addTopLevelItem(type_node)

            for schema in sorted(type_bucket, key=lambda s: (s is None, s or "")):
                schema_label = schema if schema else "(без схемы)"
                schema_node = QTreeWidgetItem([schema_label, ""])
                type_node.addChild(schema_node)

                for op in type_bucket[schema]:
                    name = op.object_name or op.object_key
                    leaf = QTreeWidgetItem([name, op.classification.value])
                    leaf.setData(0, _CLASS_ROLE, op.classification.value)
                    leaf.setData(0, _OPERATION_ROLE, op)
                    leaf.setData(0, _IS_OPERATION_ROLE, True)
                    color = QColor(_CLASSIFICATION_COLORS.get(op.classification, "#000000"))
                    leaf.setForeground(1, color)
                    leaf.setForeground(0, color)
                    schema_node.addChild(leaf)

            type_node.setExpanded(True)
            if type_node.childCount():
                type_node.child(0).setExpanded(True)

    def _populate_summary(self, plan: DeltaPlan) -> None:
        parts = [
            f"safe: {len(plan.safe_ops)}",
            f"needs-pre: {len(plan.needs_pre_ops)}",
            f"blocked: {len(plan.violations)}",
        ]
        header = (
            f"<b>db_type={plan.db_type}</b> "
            f"&middot; source_version={plan.source_version or '—'} "
            f"&rarr; target_version={plan.target_version or '—'} "
            f"&middot; {' · '.join(parts)}"
        )
        self._summary_label.setText(header)

    # --- selection / detail ---

    def _on_tree_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        if not item.data(0, _IS_OPERATION_ROLE):
            return
        op: PlannedOperation = item.data(0, _OPERATION_ROLE)
        self._show_details(op)
        self._show_ddl(op)

    def _show_details(self, op: PlannedOperation) -> None:
        f = self._detail_fields
        f["object_key"].setText(op.object_key or "—")
        f["object_schema"].setText(op.object_schema or "—")
        f["object_name"].setText(op.object_name or "—")
        f["object_type"].setText(op.object_type or "—")
        f["action"].setText(op.action or "—")
        f["classification"].setText(op.classification.value)
        rows = op.estimated_rows
        f["estimated_rows"].setText("—" if rows is None else f"{rows:,}".replace(",", " "))
        f["covered_by"].setText(", ".join(op.covered_by) if op.covered_by else "—")
        f["script_file"].setText(op.script_file or "—")
        f["reason"].setText(op.reason or "—")

    def _show_ddl(self, op: PlannedOperation) -> None:
        """Render the per-operation DDL artifact (``script_file``) with SQL highlighting.

        ``script_file`` is a relative path produced by ``DeltaService.write_artifacts``
        — typically ``delta/NNN_<type>_<schema>_<name>.sql``. The base directory is
        ``self._output_dir`` (set by ``load_from_path`` or by the caller).
        """
        if not op.script_file:
            self._ddl_view.setPlainText("(нет script_file)")
            return
        if not self._plan_path:
            self._ddl_view.setPlainText("(нет базы для script_file)")
            return
        # script_file is written next to plan.json (both are written by the same
        # _run_pipeline into the same output_dir).
        script_path = self._plan_path.parent / op.script_file
        if not script_path.exists():
            self._ddl_view.setPlainText(f"(файл не найден: {script_path})")
            return
        try:
            text = script_path.read_text(encoding="utf-8")
        except OSError as e:
            self._ddl_view.setPlainText(f"(ошибка чтения {script_path}: {e})")
            return
        # Re-attach the highlighter to the new document — the old one is on the
        # previous QPlainTextEdit.document(), which is still alive but shows
        # stale text otherwise. Easiest correct approach: drop the old highlighter
        # and create a new one bound to the current document.
        self._ddl_view.setPlainText(text)
        # Attach highlighter as an attribute so it is not garbage-collected
        # mid-render (LESSONS §42 analogue for non-Qt objects).
        self._ddl_highlighter = SqlHighlighter(self._ddl_view.document(), diff_mode=True)

    # --- filters ---

    def _on_filter_changed(self) -> None:
        self._class_filter = {cls.value: act.isChecked() for cls, act in self._class_actions.items()}
        self._apply_filters()

    def _apply_filters(self) -> None:
        root = self._tree.invisibleRootItem()
        for i in range(root.childCount()):
            type_node = root.child(i)
            for j in range(type_node.childCount()):
                schema_node = type_node.child(j)
                any_visible = False
                for k in range(schema_node.childCount()):
                    leaf = schema_node.child(k)
                    cls_value: str = leaf.data(0, _CLASS_ROLE) or ""
                    show = self._class_filter.get(cls_value, True)
                    leaf.setHidden(not show)
                    if show:
                        any_visible = True
                schema_node.setHidden(not any_visible)
            type_any = any(
                not type_node.child(j).isHidden() for j in range(type_node.childCount())
            )
            type_node.setHidden(not type_any)

    # --- chain to DeployApplyDialog (PRE-3) ---

    def _on_apply(self) -> None:
        if not self._connection_store:
            QMessageBox.information(
                self, "Plan Viewer", "Нет подключения — откройте план через диалог plan/apply."
            )
            return
        if not self._target_connection:
            QMessageBox.information(
                self, "Plan Viewer", "Не задан target_connection — откройте план через диалог plan/apply."
            )
            return
        from db_project_manager.presentation.gui.actions.dialogs import DeployApplyDialog
        from db_project_manager.presentation.gui.actions.models import DeployApplySettings

        seed = DeployApplySettings(
            codebase_dir=self._codebase_dir,
            target_connection=self._target_connection,
            output_dir=self._output_dir,
        )
        dlg = DeployApplyDialog(self._connection_store, seed, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            settings = dlg.settings()
            self._trigger_apply(settings)

    def _trigger_apply(self, settings) -> None:
        """Hand the accepted DeployApplySettings to the main window.

        Implementation lives in main_window (S4): it knows how to wire
        DeployApplyWorker through its execute pipeline. We use a custom signal
        so this class stays decoupled from main_window internals.
        """
        # Defer to main_window via a Qt signal: emit on the window itself is
        # not appropriate (MainWindow expects a callback), so we expose a
        # simple hook attribute that main_window replaces at registration
        # time. If nothing is hooked, we fall back to a QMessageBox asking
        # the user to use the action panel.
        hook = getattr(self, "_on_apply_accepted", None)
        if hook is None:
            QMessageBox.information(
                self,
                "Plan Viewer",
                "Запустите «Применить деплой к существующей БД» из панели действий с теми же параметрами.",
            )
            return
        hook(settings)
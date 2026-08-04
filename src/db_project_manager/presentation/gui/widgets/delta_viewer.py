"""Delta Viewer window — browse a ``diff_report.json`` interactively (Phase 14).

A standalone ``QMainWindow`` that opens an existing ``diff_report.json`` (produced by
``db-pm compare run``) and presents it as a tree of objects grouped by type/schema,
color-coded by status (added/removed/changed/unchanged), with a detail panel and a
unified-diff view for changed DDL.

Design (see ``-=tasks=-/phase_14/Phase_14_vision_final.md`` §4.3):
    - Reads a single self-describing ``diff_report.json`` (both snapshots are inside).
    - Separate window (not a dock — the main window has no docks; not an action dialog
      — those close on OK). Opened from the main window's menu, lives until closed.
    - Worker for parsing the JSON off the UI thread (large schemas). Signals connected
      to bound methods only; the worker is held by a strong ref until ``finished``
      (LESSONS §42).
    - Selection of objects via checkboxes is saved to a local ``selection.json``
      artifact (no CD pipeline coupling until Phase 12 — _final DV-3).
"""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import Qt, QThreadPool
from PySide6.QtGui import QAction, QColor
from PySide6.QtWidgets import (
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
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

from db_project_manager.domain.diff import DiffEntry, DiffReport, DiffStatus
from db_project_manager.infrastructure.diff.grouping import group_entries_by_status, type_priority
from db_project_manager.infrastructure.diff.markdown_report import (
    unified_diff_text,
    write_diff_markdown,
)
from db_project_manager.presentation.gui.widgets.sql_highlighter import SqlHighlighter
from db_project_manager.presentation.gui.widgets.workers import LoadDiffReportWorker

#: Display order of statuses in the filter row and summary.
_STATUS_ORDER = (
    DiffStatus.ADDED,
    DiffStatus.REMOVED,
    DiffStatus.CHANGED,
    DiffStatus.UNCHANGED,
)

#: Colors per status (DV-7). Dark-theme-oriented hex codes that also read on light.
_STATUS_COLORS: dict[DiffStatus, str] = {
    DiffStatus.ADDED: "#2EA043",     # green
    DiffStatus.REMOVED: "#F85149",   # red
    DiffStatus.CHANGED: "#D29922",   # yellow/amber
    DiffStatus.UNCHANGED: "#8B949E", # gray
}

#: Qt item-data role carrying the DiffStatus of a tree node.
_STATUS_ROLE = Qt.ItemDataRole.UserRole
#: Qt item-data role carrying the object_key of a leaf node.
_OBJECT_KEY_ROLE = Qt.ItemDataRole.UserRole + 1
#: Qt item-data role marking a node as an object leaf (vs group nodes).
_IS_OBJECT_ROLE = Qt.ItemDataRole.UserRole + 2


class DeltaViewerWindow(QMainWindow):
    """Browse a ``diff_report.json`` as a tree + detail + diff tabs."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Delta Viewer")
        self.setMinimumSize(1000, 700)

        self.thread_pool = QThreadPool.globalInstance()
        # Strong refs to the load worker (LESSONS §42 — held until 'finished').
        self._active_workers: dict = {}
        # Last loaded report (for markdown export / selection save).
        self._report: DiffReport | None = None
        # Path the report was loaded from (for writing selection.json / markdown next to it).
        self._report_path: Path | None = None
        # Active status filters keyed by status.value (str). All shown by default.
        # Kept as plain strings because PySide6 does not preserve enum identity across
        # QTreeWidgetItem.setData round-trip (a str-Enum comes back as a plain str), so
        # comparing by value is the only stable option.
        self._status_filter: dict[str, bool] = {s.value: True for s in _STATUS_ORDER}
        # Current search substring (case-insensitive).
        self._search_text: str = ""

        self._init_ui()

    # --- UI construction ---

    def _init_ui(self) -> None:
        self._build_toolbar()

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        # Summary bar (filled when a report is loaded).
        self._summary_label = QLabel("(откройте diff_report.json)")
        self._summary_label.setStyleSheet("padding: 4px; color: gray;")
        root.addWidget(self._summary_label)

        # Filter row: status checkboxes + search field.
        filter_row = QHBoxLayout()
        self._status_checks: dict[DiffStatus, QAction] = {}
        for status in _STATUS_ORDER:
            act = QAction(_status_label(status), self)
            act.setCheckable(True)
            act.setChecked(True)
            # Color the label via a small swatch in the tooltip; QAction text coloring
            # is platform-limited, so we keep the plain label and rely on tree coloring.
            act.setToolTip(f"Показывать объекты со статусом «{status.value}»")
            act.triggered.connect(self._on_filter_changed)
            self._status_checks[status] = act
            # Wrap each action in a checkable button so it's visible in the filter row.
            from PySide6.QtWidgets import QToolButton
            btn = QToolButton()
            btn.setDefaultAction(act)
            btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
            filter_row.addWidget(btn)

        filter_row.addWidget(QLabel("Поиск:"))
        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText("имя объекта…")
        self._search_edit.textChanged.connect(self._on_search_changed)
        filter_row.addWidget(self._search_edit, stretch=1)
        root.addLayout(filter_row)

        # Main splitter: tree | detail tabs.
        self._splitter = QSplitter(Qt.Orientation.Horizontal)

        self._tree = QTreeWidget()
        self._tree.setHeaderLabels(["Объект", "Статус"])
        self._tree.setColumnWidth(0, 420)
        self._tree.itemClicked.connect(self._on_tree_clicked)
        self._splitter.addWidget(self._tree)

        # Right: detail tabs.
        tabs = QTabWidget()

        # --- Details tab ---
        details_widget = QWidget()
        details_layout = QFormLayout(details_widget)
        self._detail_fields: dict[str, QLabel] = {}
        for field in ("object_key", "object_schema", "object_name", "object_type",
                      "object_signature", "estimated_rows", "status"):
            lbl = QLabel("—")
            lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self._detail_fields[field] = lbl
            details_layout.addRow(f"{field}:", lbl)
        self._detail_fields["sql_normalized_source"] = QLabel("—")
        tabs.addTab(details_widget, "Детали")

        # --- Diff tab ---
        diff_widget = QWidget()
        diff_layout = QVBoxLayout(diff_widget)
        diff_layout.setContentsMargins(0, 0, 0, 0)
        self._diff_view = QPlainTextEdit()
        self._diff_view.setReadOnly(True)
        self._diff_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        font = self._diff_view.font()
        font.setFamily("Consolas")
        font.setPointSize(10)
        self._diff_view.setFont(font)
        diff_layout.addWidget(self._diff_view)
        tabs.addTab(diff_widget, "Diff")
        # Attach the diff-mode highlighter to the diff view's document.
        self._diff_highlighter = SqlHighlighter(self._diff_view.document(), diff_mode=True)

        self._splitter.addWidget(tabs)
        self._splitter.setStretchFactor(0, 1)
        self._splitter.setStretchFactor(1, 2)

        root.addWidget(self._splitter, stretch=1)

        # --- buttons row (added last per LESSONS §43) ---
        from PySide6.QtWidgets import QPushButton
        button_row = QHBoxLayout()
        self._save_selection_btn = QPushButton("Сохранить выбор…")
        self._save_selection_btn.setToolTip("Записать выбранные object_key в selection.json")
        self._save_selection_btn.clicked.connect(self._on_save_selection)
        self._save_selection_btn.setEnabled(False)
        button_row.addStretch(1)
        button_row.addWidget(self._save_selection_btn)
        root.addLayout(button_row)

    def _build_toolbar(self) -> None:
        tb = self.addToolBar("Delta")
        tb.setMovable(False)

        open_act = QAction("Открыть JSON…", self)
        open_act.setToolTip("Выбрать diff_report.json для просмотра")
        open_act.triggered.connect(self._on_open_json)
        tb.addAction(open_act)

        export_act = QAction("Экспорт markdown…", self)
        export_act.setToolTip("Сохранить diff_report.md рядом с отчётом")
        export_act.triggered.connect(self._on_export_markdown)
        tb.addAction(export_act)

    # --- loading ---

    def load_from_path(self, path: str | Path) -> None:
        """Asynchronously load a ``diff_report.json`` (off the UI thread)."""
        path = Path(path)
        self._report_path = path
        self._summary_label.setText(f"Загрузка: {path.name} …")

        worker = LoadDiffReportWorker(path)
        # bound methods only (LESSONS §42 — no lambda/partial in connect).
        worker.signals.status.connect(self._on_load_status)
        worker.signals.error.connect(self._on_load_error)
        worker.signals.finished.connect(self._on_load_finished)
        self._active_workers[worker.signals] = worker
        self.thread_pool.start(worker)

    def show_report(self, report: DiffReport, *, path: str | Path | None = None) -> None:
        """Populate the tree from an already-parsed report (synchronous, test-friendly).

        Used by tests and by :meth:`_on_load_finished` after the worker parses the JSON.
        """
        self._report = report
        if path is not None:
            self._report_path = Path(path)
        self._populate_tree(report)
        self._populate_summary(report)
        self._save_selection_btn.setEnabled(True)

    # --- tree population ---

    def _populate_tree(self, report: DiffReport) -> None:
        """Build the grouped tree (type -> schema -> object) from report entries."""
        self._tree.clear()

        # Group ALL statuses together by type/schema so the tree shows the whole layout;
        # filtering then hides/shows leaf nodes. (Group nodes hide themselves when all
        # their children are hidden — see _apply_filters.)
        by_type: dict[str, dict[str | None, list[DiffEntry]]] = {}
        for status in _STATUS_ORDER:
            for object_type, by_schema in group_entries_by_status(report.entries, status).items():
                type_bucket = by_type.setdefault(object_type, {})
                for schema, entries in by_schema.items():
                    type_bucket.setdefault(schema, []).extend(entries)

        for object_type in sorted(by_type, key=type_priority):
            type_node = QTreeWidgetItem([f"{object_type} ({_count_type(report, object_type)})", ""])
            type_node.setFont(0, type_node.font(0))  # placeholder; bold set below
            f = type_node.font(0)
            f.setBold(True)
            type_node.setFont(0, f)
            self._tree.addTopLevelItem(type_node)

            type_bucket = by_type[object_type]
            for schema in sorted(type_bucket, key=lambda s: (s is None, s or "")):
                schema_label = schema if schema else "(без схемы)"
                schema_node = QTreeWidgetItem([schema_label, ""])
                type_node.addChild(schema_node)

                for entry in sorted(type_bucket[schema], key=lambda e: _entry_name(e)):
                    name = _entry_name(entry)
                    leaf = QTreeWidgetItem([name, entry.status.value])
                    # Store the status as its .value (str) — PySide6 does not preserve
                    # enum identity across QTreeWidgetItem.setData round-trip, so the
                    # filter logic and tests must compare by string value.
                    leaf.setData(0, _STATUS_ROLE, entry.status.value)
                    leaf.setData(0, _OBJECT_KEY_ROLE, entry.object_key)
                    leaf.setData(0, _IS_OBJECT_ROLE, True)
                    # Checkbox for selection (DV-3); groups are not checkable.
                    leaf.setFlags(leaf.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    leaf.setCheckState(0, Qt.CheckState.Unchecked)
                    color = QColor(_STATUS_COLORS.get(entry.status, "#000000"))
                    leaf.setForeground(1, color)
                    leaf.setForeground(0, color)
                    # Stash the entry on the node for the detail/diff views.
                    leaf.setData(0, Qt.ItemDataRole.UserRole + 3, entry)
                    schema_node.addChild(leaf)

            type_node.setExpanded(True)
            # Expand the first schema under each type by default for discoverability.
            if type_node.childCount():
                type_node.child(0).setExpanded(True)

    def _populate_summary(self, report: DiffReport) -> None:
        s = report.summary
        src = report.source
        tgt = report.target
        parts = [
            f"added: {s.get('added', 0)}",
            f"removed: {s.get('removed', 0)}",
            f"changed: {s.get('changed', 0)}",
            f"unchanged: {s.get('unchanged', 0)}",
        ]
        header = (
            f"<b>{src.source_ref}</b> ({src.source_kind.value}) "
            f"&rarr; <b>{tgt.source_ref}</b> ({tgt.source_kind.value}) "
            f"&middot; {' · '.join(parts)}"
        )
        self._summary_label.setText(header)
        self._summary_label.setStyleSheet("padding: 4px;")

    # --- selection / detail ---

    def _on_tree_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        if not item.data(0, _IS_OBJECT_ROLE):
            return
        entry: DiffEntry = item.data(0, Qt.ItemDataRole.UserRole + 3)
        self._show_details(entry)
        self._show_diff(entry)

    def _show_details(self, entry: DiffEntry) -> None:
        snap = entry.source_snapshot if entry.source_snapshot is not None else entry.target_snapshot
        fields = self._detail_fields
        fields["status"].setText(entry.status.value)
        if snap is None:
            for k in ("object_key", "object_schema", "object_name", "object_type",
                      "object_signature", "estimated_rows"):
                fields[k].setText("—")
            return
        fields["object_key"].setText(snap.object_key)
        fields["object_schema"].setText(snap.object_schema or "—")
        fields["object_name"].setText(snap.object_name or "—")
        fields["object_type"].setText(snap.object_type)
        fields["object_signature"].setText(snap.object_signature or "—")
        rows = snap.estimated_rows
        fields["estimated_rows"].setText(
            "—" if rows is None or snap.object_type != "table" else f"{rows:,}".replace(",", " ")
        )

    def _show_diff(self, entry: DiffEntry) -> None:
        if entry.status is DiffStatus.CHANGED and entry.source_snapshot and entry.target_snapshot:
            text = unified_diff_text(entry.source_snapshot, entry.target_snapshot)
            self._diff_view.setPlainText(text if text.strip() else "(изменения не видны в текстовом diff)")
        elif entry.status is DiffStatus.ADDED:
            self._diff_view.setPlainText("(объект добавлен — нет target-стороны для сравнения)")
        elif entry.status is DiffStatus.REMOVED:
            self._diff_view.setPlainText("(объект удалён — нет source-стороны для сравнения)")
        else:  # unchanged
            self._diff_view.setPlainText("(без изменений)")

    # --- filters ---

    def _on_filter_changed(self) -> None:
        self._status_filter = {s: act.isChecked() for s, act in self._status_checks.items()}
        self._apply_filters()

    def _on_search_changed(self, text: str) -> None:
        self._search_text = text.strip().lower()
        self._apply_filters()

    def _apply_filters(self) -> None:
        """Hide/show tree nodes based on status checkboxes and the search field.

        Group nodes (type/schema) hide themselves when all their object children are
        hidden.
        """
        root = self._tree.invisibleRootItem()
        for i in range(root.childCount()):
            type_node = root.child(i)
            for j in range(type_node.childCount()):
                schema_node = type_node.child(j)
                any_visible = False
                for k in range(schema_node.childCount()):
                    leaf = schema_node.child(k)
                    # status.value (str) stored on the node — see _populate_tree.
                    status_value: str = leaf.data(0, _STATUS_ROLE) or ""
                    name = leaf.text(0).lower()
                    show = self._status_filter.get(status_value, True)
                    if self._search_text:
                        show = show and (self._search_text in name)
                    leaf.setHidden(not show)
                    if show:
                        any_visible = True
                schema_node.setHidden(not any_visible)
            # Type node hidden if all schema nodes hidden.
            type_any = any(
                not type_node.child(j).isHidden() for j in range(type_node.childCount())
            )
            type_node.setHidden(not type_any)

    # --- save selection (DV-3) ---

    def _on_save_selection(self) -> None:
        if self._report is None:
            return
        selected: list[str] = []
        root = self._tree.invisibleRootItem()
        for i in range(root.childCount()):
            type_node = root.child(i)
            for j in range(type_node.childCount()):
                schema_node = type_node.child(j)
                for k in range(schema_node.childCount()):
                    leaf = schema_node.child(k)
                    if leaf.checkState(0) == Qt.CheckState.Checked:
                        key = leaf.data(0, _OBJECT_KEY_ROLE)
                        if key:
                            selected.append(key)
        if not selected:
            QMessageBox.information(self, "Сохранение выбора", "Не отмечено ни одного объекта.")
            return

        out_path = self._selection_path()
        try:
            out_path.write_text(
                json.dumps({"selected": sorted(selected)}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as e:
            QMessageBox.critical(self, "Сохранение выбора", f"Не удалось записать файл: {e}")
            return
        QMessageBox.information(
            self, "Сохранение выбора",
            f"Сохранено {len(selected)} объектов в:\n{out_path}",
        )

    def _selection_path(self) -> Path:
        """Where to write selection.json (next to the report, or CWD fallback)."""
        if self._report_path is not None:
            return self._report_path.parent / "selection.json"
        return Path("selection.json")

    # --- toolbar actions ---

    def _on_open_json(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Открыть diff_report.json", "", "JSON (*.json)"
        )
        if path:
            self.load_from_path(path)

    def _on_export_markdown(self) -> None:
        if self._report is None:
            QMessageBox.information(self, "Экспорт markdown", "Сначала откройте отчёт.")
            return
        suggested = self._report_path.parent / "diff_report.md" if self._report_path else None
        path, _ = QFileDialog.getSaveFileName(
            self, "Сохранить markdown", str(suggested) if suggested else "", "Markdown (*.md)"
        )
        if not path:
            return
        try:
            out = write_diff_markdown(self._report, output=path)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Экспорт markdown", f"Не удалось записать файл: {e}")
            return
        QMessageBox.information(self, "Экспорт markdown", f"Сохранено:\n{out}")

    # --- worker slots (bound methods only — LESSONS §42) ---

    def _on_load_status(self, message: str) -> None:
        self._summary_label.setText(message)

    def _on_load_error(self, message: str) -> None:
        self._summary_label.setText(f"Ошибка: {message}")
        self._report = None
        QMessageBox.critical(self, "Загрузка отчёта", message)

    def _on_load_finished(self, result) -> None:
        self._active_workers.pop(self.sender(), None)
        if result is None:
            return  # error already reported via _on_load_error
        self.show_report(result, path=self._report_path)


# --- helpers ---


def _status_label(status: DiffStatus) -> str:
    return {
        DiffStatus.ADDED: "added",
        DiffStatus.REMOVED: "removed",
        DiffStatus.CHANGED: "changed",
        DiffStatus.UNCHANGED: "unchanged",
    }[status]


def _entry_name(entry: DiffEntry) -> str:
    snap = entry.source_snapshot if entry.source_snapshot is not None else entry.target_snapshot
    return snap.object_name if snap and snap.object_name else entry.object_key


def _count_type(report: DiffReport, object_type: str) -> int:
    n = 0
    for e in report.entries:
        snap = e.source_snapshot if e.source_snapshot is not None else e.target_snapshot
        if snap is not None and snap.object_type == object_type:
            n += 1
    return n

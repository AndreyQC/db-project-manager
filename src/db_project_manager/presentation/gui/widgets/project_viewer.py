"""Project tree + read-only SQL preview panel.

Layout: QSplitter with a QTreeView (filesystem model of the output dir) on the
left and a read-only QPlainTextEdit with SQL highlighting on the right.
Clicking a .sql file reads it off the event loop (small files) and shows it.

Phase 1 uses QPlainTextEdit + QSyntaxHighlighter (no extra deps). Scintilla
(folding, richer highlighting) is a Phase 2 option.
"""

from __future__ import annotations

from pathlib import Path

from loguru import logger
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFileSystemModel,
    QLabel,
    QPlainTextEdit,
    QSplitter,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from db_project_manager.presentation.gui.widgets.sql_highlighter import SqlHighlighter


class ProjectViewer(QWidget):
    """Read-only browser of generated SQL files."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._highlighter: SqlHighlighter | None = None
        self._init_ui()

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._splitter = QSplitter(Qt.Orientation.Horizontal)

        # --- tree ---
        self._tree = QTreeView()
        self._fs_model = QFileSystemModel()
        self._fs_model.setRootPath("")  # will be set via set_root
        self._tree.setModel(self._fs_model)
        self._tree.setHeaderHidden(True)
        self._tree.hideColumn(1)  # size
        self._tree.hideColumn(2)  # type
        self._tree.hideColumn(3)  # date
        self._tree.clicked.connect(self._on_clicked)
        self._splitter.addWidget(self._tree)

        # --- editor ---
        editor_container = QWidget()
        editor_layout = QVBoxLayout(editor_container)
        editor_layout.setContentsMargins(0, 0, 0, 0)
        self._path_label = QLabel("(выберите файл в дереве)")
        self._path_label.setStyleSheet("color: gray; padding: 2px;")
        editor_layout.addWidget(self._path_label)

        self._editor = QPlainTextEdit()
        self._editor.setReadOnly(True)
        self._editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        font = self._editor.font()
        font.setFamily("Consolas")
        font.setPointSize(10)
        self._editor.setFont(font)
        # Monospace + line numbers via QTextEdit built-in counter is not available
        # on QPlainTextEdit directly; we keep a block-count display in the status.
        editor_layout.addWidget(self._editor)

        self._splitter.addWidget(editor_container)
        self._splitter.setStretchFactor(0, 1)
        self._splitter.setStretchFactor(1, 3)

        layout.addWidget(self._splitter)

        # Attach highlighter to the editor's document.
        self._highlighter = SqlHighlighter(self._editor.document())

    def set_root(self, path: str | Path) -> None:
        """Point the tree at a generated project directory."""
        path = str(path)
        index = self._fs_model.setRootPath(path)
        self._tree.setRootIndex(index)
        self._tree.setExpanded(self._fs_model.index(path), True)

    def load_file(self, path: str | Path) -> None:
        """Load a file into the preview editor (read-only)."""
        try:
            text = Path(path).read_text(encoding="utf-8-sig")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Не удалось прочитать файл {path}: {e}")
            text = f"(не удалось прочитать файл: {e})"
        self._editor.setPlainText(text)
        self._path_label.setText(str(path))

    def splitter_state(self) -> bytes:
        return self._splitter.saveState()

    def restore_splitter_state(self, state: bytes) -> None:
        self._splitter.restoreState(state)

    def _on_clicked(self, index) -> None:
        path = self._fs_model.filePath(index)
        if path and Path(path).is_file() and path.lower().endswith(".sql"):
            self.load_file(path)

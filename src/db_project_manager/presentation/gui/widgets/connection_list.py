"""List widget showing available connections (from connections/*.yaml)."""

from __future__ import annotations

from PySide6.QtCore import QAbstractListModel, QModelIndex, Qt
from PySide6.QtWidgets import QAbstractItemView, QListView, QPushButton, QVBoxLayout, QWidget


class ConnectionListModel(QAbstractListModel):
    def __init__(self, names: list[str] | None = None) -> None:
        super().__init__()
        self.names = names or []

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802 (Qt override)
        return len(self.names)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if not index.isValid() or role != Qt.ItemDataRole.DisplayRole:
            return None
        return self.names[index.row()]

    def refresh(self, names: list[str]) -> None:
        self.beginResetModel()
        self.names = list(names)
        self.endResetModel()


class ConnectionListWidget(QWidget):
    """A list with Add/Edit/Delete buttons."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.view = QListView()
        self.view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.model = ConnectionListModel()
        self.view.setModel(self.model)
        layout.addWidget(self.view)

        self.add_btn = QPushButton("Добавить…")
        self.edit_btn = QPushButton("Изменить…")
        self.delete_btn = QPushButton("Удалить")
        for b in (self.add_btn, self.edit_btn, self.delete_btn):
            layout.addWidget(b)

    def refresh(self, names: list[str]) -> None:
        self.model.refresh(names)

    def selected_name(self) -> str | None:
        indexes = self.view.selectedIndexes()
        if not indexes:
            return None
        return self.model.data(indexes[0], Qt.ItemDataRole.DisplayRole)

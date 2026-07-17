"""Background workers for long-running GUI operations.

Uses QRunnable + QThreadPool with explicit signals (no nested QEventLoop):
the main window connects to signals.status/error/finished and updates widgets,
keeping the UI responsive.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Signal

from db_project_manager.application.reverse_engineer import (
    ReverseEngineerError,
    build_default_service,
)
from db_project_manager.domain.connection import ConnectionConfig


class WorkerSignals(QObject):
    progress = Signal(str, int, int)  # message, current, total
    status = Signal(str)
    error = Signal(str)
    finished = Signal(object)  # result (Path) or None on error


class ReverseEngineerWorker(QRunnable):
    """Run reverse engineering off the UI thread."""

    def __init__(self, conn_cfg: ConnectionConfig, output_dir: str | Path) -> None:
        super().__init__()
        self.conn_cfg = conn_cfg
        self.output_dir = Path(output_dir)
        self.signals = WorkerSignals()

    def run(self) -> None:  # noqa: C901 (Qt entrypoint)
        service = build_default_service()

        def progress(message: str, current: int, total: int) -> None:
            self.signals.progress.emit(message, current, total)
            self.signals.status.emit(message)

        try:
            result = service.run(self.conn_cfg, self.output_dir, progress=progress)
            self.signals.status.emit(f"Готово: {result}")
            self.signals.finished.emit(result)
        except ReverseEngineerError as e:
            self.signals.error.emit(str(e))
            self.signals.finished.emit(None)
        except Exception as e:  # noqa: BLE001
            self.signals.error.emit(f"Непредвиденная ошибка: {e}")
            self.signals.finished.emit(None)

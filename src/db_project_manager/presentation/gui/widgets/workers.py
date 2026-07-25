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


class DeployValidateWorker(QRunnable):
    """Run validation deploy off the UI thread."""

    def __init__(
        self,
        conn_cfg: ConnectionConfig,
        codebase_dir: str | Path,
        *,
        prefix: str | None = None,
        keep_db: bool = False,
        continue_on_error: bool = False,
    ) -> None:
        super().__init__()
        self.conn_cfg = conn_cfg
        self.codebase_dir = Path(codebase_dir)
        self.prefix = prefix
        self.keep_db = keep_db
        self.continue_on_error = continue_on_error
        self.signals = WorkerSignals()

    def run(self) -> None:  # noqa: C901 (Qt entrypoint)
        from db_project_manager.application.deploy_service import (
            DeployPermissionError,
            DeployValidateService,
        )
        from db_project_manager.domain.graph import CycleError

        service = DeployValidateService()

        def progress(message: str, current: int, total: int) -> None:
            self.signals.progress.emit(message, current, total)
            self.signals.status.emit(message)

        try:
            result = service.run(
                self.conn_cfg,
                self.codebase_dir,
                prefix=self.prefix,
                keep_db=self.keep_db,
                continue_on_error=self.continue_on_error,
                progress=progress,
            )
            # Pass the structured result through 'finished' (object).
            self.signals.finished.emit(result)
        except DeployPermissionError as e:
            self.signals.error.emit(f"Нет прав CREATEDB: {e}")
            self.signals.finished.emit(None)
        except CycleError as e:
            self.signals.error.emit(f"Граф содержит циклы: {', '.join(sorted(e.unresolved))}")
            self.signals.finished.emit(None)
        except Exception as e:  # noqa: BLE001
            self.signals.error.emit(f"Непредвиденная ошибка: {e}")
            self.signals.finished.emit(None)

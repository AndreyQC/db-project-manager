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


class GraphBuildWorker(QRunnable):
    """Build the dependency graph (optionally export + validate) off the UI thread."""

    def __init__(
        self,
        codebase_dir: str | Path,
        *,
        fmt: str = "graphml",
        validate: bool = True,
        output_dir: str | Path | None = None,
    ) -> None:
        super().__init__()
        self.codebase_dir = Path(codebase_dir)
        self.fmt = fmt
        self.validate = validate
        # Optional export destination; None = <codebase>/.dbm_graph/.
        self.output_dir = Path(output_dir) if output_dir else None
        self.signals = WorkerSignals()

    def run(self) -> None:  # noqa: C901 (Qt entrypoint)
        from db_project_manager.application.graph_service import BuildGraphService
        from db_project_manager.domain.graph import CycleError
        from db_project_manager.infrastructure.graph.export import export_graph
        from db_project_manager.infrastructure.graph.topological_sort import topological_sort

        try:
            service = BuildGraphService()
            self.signals.status.emit(f"Построение графа: {self.codebase_dir}")
            graph_dir = service.build_and_store(self.codebase_dir)
            graph = service.build(self.codebase_dir)
            self.signals.status.emit(
                f"Граф построен: вершин={len(graph.vertices)}, рёбер={len(graph.edges)}; {graph_dir}"
            )

            if self.validate:
                topological_sort(graph)  # raises CycleError on cycles
                dangling = graph.dangling_edges()
                if dangling:
                    names = ", ".join(
                        f"{e.source_object_key} -> {e.destination_object_key}" for e in dangling[:20]
                    )
                    raise ValueError(f"Висячие ссылки ({len(dangling)}): {names}")
                self.signals.status.emit("Граф валиден: циклов и висячих ссылок нет.")

            result: Path = graph_dir
            if self.fmt != "none":
                export_dir = self.output_dir or graph_dir
                export_dir.mkdir(parents=True, exist_ok=True)
                result = export_dir / f"graph.{self.fmt}"
                export_graph(graph, self.fmt, result)
                self.signals.status.emit(f"Экспорт графа ({self.fmt}): {result}")

            self.signals.finished.emit(result)
        except CycleError as e:
            self.signals.error.emit(f"Граф содержит циклы: {', '.join(sorted(e.unresolved))}")
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


class DeployAnalyzeWorker(QRunnable):
    """Run the safety-gate dry-run off the UI thread (Phase 11, SG-7).

    Read-only with respect to the target DB: SafetyGateService never calls a
    mutating adapter method. Emits the SafetyGateVerdict through 'finished'
    (object); SafetyGateError and unexpected errors go via signals.error with
    finished(None) — the same contract as DeployValidateWorker.
    """

    def __init__(
        self,
        conn_cfg: ConnectionConfig,
        codebase_dir: str | Path,
        output_dir: str | Path,
    ) -> None:
        super().__init__()
        self.conn_cfg = conn_cfg
        self.codebase_dir = Path(codebase_dir)
        self.output_dir = Path(output_dir)
        self.signals = WorkerSignals()

    def run(self) -> None:
        from db_project_manager.application.safety_gate_service import (
            SafetyGateError,
            SafetyGateService,
        )

        service = SafetyGateService()

        def progress(message: str, current: int, total: int) -> None:
            self.signals.progress.emit(message, current, total)
            self.signals.status.emit(message)

        try:
            verdict = service.analyze(
                self.codebase_dir, self.conn_cfg, self.output_dir, progress=progress
            )
            self.signals.finished.emit(verdict)
        except SafetyGateError as e:
            self.signals.error.emit(f"Safety gate: {e}")
            self.signals.finished.emit(None)
        except Exception as e:  # noqa: BLE001
            self.signals.error.emit(f"Непредвиденная ошибка: {e}")
            self.signals.finished.emit(None)


class CompareWorker(QRunnable):
    """Run a DB/codebase comparison off the UI thread.

    Delegates to CompareService.run; emits the output-dir Path on success, None on
    error. CompareError (db_type mismatch, missing manifest) is reported via
    signals.error with its message.
    """

    def __init__(
        self,
        source,  # SideSpec
        target,  # SideSpec
        output_dir: str | Path,
        *,
        keep_model_dir: bool = False,
    ) -> None:
        super().__init__()
        self.source = source
        self.target = target
        self.output_dir = Path(output_dir)
        self.keep_model_dir = keep_model_dir
        self.signals = WorkerSignals()

    def run(self) -> None:  # noqa: C901 (Qt entrypoint)
        from db_project_manager.application.compare_service import CompareError, CompareService

        service = CompareService()

        def progress(message: str, current: int, total: int) -> None:
            self.signals.progress.emit(message, current, total)
            self.signals.status.emit(message)

        try:
            result = service.run(
                self.source,
                self.target,
                self.output_dir,
                keep_model_dir=self.keep_model_dir,
                progress=progress,
            )
            self.signals.status.emit(f"Сравнение завершено: {result}")
            self.signals.finished.emit(result)
        except CompareError as e:
            self.signals.error.emit(str(e))
            self.signals.finished.emit(None)
        except Exception as e:  # noqa: BLE001
            self.signals.error.emit(f"Непредвиденная ошибка: {e}")
            self.signals.finished.emit(None)


class LoadDiffReportWorker(QRunnable):
    """Load + parse a ``diff_report.json`` off the UI thread (Phase 14).

    Used by :class:`DeltaViewerWindow` to keep large-file parsing off the UI thread.
    Emits the parsed :class:`~db_project_manager.domain.diff.DiffReport` on success,
    or ``None`` + an error message on failure (missing/invalid file).
    """

    def __init__(self, path: str | Path) -> None:
        super().__init__()
        self.path = Path(path)
        self.signals = WorkerSignals()

    def run(self) -> None:  # noqa: C901 (Qt entrypoint)
        from db_project_manager.domain.diff import DiffReport
        from pydantic import ValidationError

        try:
            text = self.path.read_text(encoding="utf-8")
            report = DiffReport.model_validate_json(text)
            self.signals.status.emit(f"Отчёт загружен: {self.path.name}")
            self.signals.finished.emit(report)
        except (ValidationError, ValueError) as e:
            self.signals.error.emit(f"Не удалось разобрать отчёт: {e}")
            self.signals.finished.emit(None)
        except OSError as e:
            self.signals.error.emit(f"Не удалось прочитать файл: {e}")
            self.signals.finished.emit(None)
        except Exception as e:  # noqa: BLE001
            self.signals.error.emit(f"Непредвиденная ошибка: {e}")
            self.signals.finished.emit(None)

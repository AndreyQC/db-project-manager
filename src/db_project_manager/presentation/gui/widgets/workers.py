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
from db_project_manager.infrastructure.deploy.canonical_ddl import DEFAULT_SERVICE_SCHEMA


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


class DeployPlanWorker(QRunnable):
    """Run ``db-pm deploy plan`` (dry-run delta) off the UI thread (Phase 15, PRE-3).

    Mirrors ``DeployAnalyzeWorker``: ``DeployApplyService.plan()`` is read-only
    (no mutation of the target DB). Emits a ``DeltaPlan`` through ``finished``;
    ``DeployApplyRejected`` and ``DeployApplyError`` go via ``signals.error`` with
    ``finished(None)``.
    """

    def __init__(
        self,
        conn_cfg: ConnectionConfig,
        codebase_dir: str | Path,
        output_dir: str | Path,
        *,
        include_drops: bool = False,
    ) -> None:
        super().__init__()
        self.conn_cfg = conn_cfg
        self.codebase_dir = Path(codebase_dir)
        self.output_dir = Path(output_dir)
        self.include_drops = include_drops
        self.signals = WorkerSignals()

    def run(self) -> None:
        from db_project_manager.application.deploy_apply_service import (
            DeployApplyError,
            DeployApplyRejected,
            DeployApplyService,
        )

        service = DeployApplyService()

        def progress(message: str, current: int, total: int) -> None:
            self.signals.progress.emit(message, current, total)
            self.signals.status.emit(message)

        try:
            plan = service.plan(
                self.codebase_dir,
                self.conn_cfg,
                self.output_dir,
                include_drops=self.include_drops,
                progress=progress,
            )
            self.signals.status.emit(
                f"План готов: операций {len(plan.operations)} "
                f"(safe: {len(plan.safe_ops)}, needs-pre: {len(plan.needs_pre_ops)}, "
                f"blocked: {len(plan.violations)})"
            )
            self.signals.finished.emit(plan)
        except DeployApplyRejected as e:
            self.signals.error.emit(f"Safety gate отклонил план: {e}")
            self.signals.finished.emit(None)
        except DeployApplyError as e:
            self.signals.error.emit(f"Plan: {e}")
            self.signals.finished.emit(None)
        except Exception as e:  # noqa: BLE001
            self.signals.error.emit(f"Непредвиденная ошибка: {e}")
            self.signals.finished.emit(None)


class DeployApplyWorker(QRunnable):
    """Run ``db-pm deploy apply`` (mutates an EXISTING target DB) off the UI thread.

    This is the FIRST worker that mutates a live database — see the preflight
    ``confirm_understands_risk`` gate in ``DeployApplyDialog``. Mirrors
    ``DeployAnalyzeWorker`` for error handling: ``DeployApplyRejected`` and
    ``DeployApplyError`` go via ``signals.error`` with ``finished(None)``;
    success emits an ``ApplyResult`` through ``finished``.
    """

    def __init__(
        self,
        conn_cfg: ConnectionConfig,
        codebase_dir: str | Path,
        output_dir: str | Path,
        *,
        include_drops: bool = False,
        no_rehearsal: bool = False,
        keep_rehearsal_db: bool = False,
    ) -> None:
        super().__init__()
        self.conn_cfg = conn_cfg
        self.codebase_dir = Path(codebase_dir)
        self.output_dir = Path(output_dir)
        self.include_drops = include_drops
        self.no_rehearsal = no_rehearsal
        self.keep_rehearsal_db = keep_rehearsal_db
        self.signals = WorkerSignals()

    def run(self) -> None:
        from db_project_manager.application.deploy_apply_service import (
            DeployApplyError,
            DeployApplyRejected,
            DeployApplyService,
        )

        service = DeployApplyService()

        def progress(message: str, current: int, total: int) -> None:
            self.signals.progress.emit(message, current, total)
            self.signals.status.emit(message)

        try:
            result = service.apply(
                self.codebase_dir,
                self.conn_cfg,
                self.output_dir,
                include_drops=self.include_drops,
                rehearsal=not self.no_rehearsal,
                keep_rehearsal_db=self.keep_rehearsal_db,
                progress=progress,
            )
            self.signals.status.emit(
                f"✓ Apply завершён: {result.applied}/{result.planned} операций, "
                f"версия {result.applied_version}"
            )
            self.signals.finished.emit(result)
        except DeployApplyRejected as e:
            self.signals.error.emit(f"Apply отклонён safety gate: {e}")
            self.signals.finished.emit(None)
        except DeployApplyError as e:
            self.signals.error.emit(f"Apply: {e}")
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


class YamlGenerateWorker(QRunnable):
    """Run db-pm yaml generate off the UI thread (Phase 13)."""

    def __init__(
        self,
        source_dir: str | Path,
        db_type: str,
        output_file: str | Path,
        source_version: str = "",
    ) -> None:
        super().__init__()
        self.source_dir = Path(source_dir)
        self.db_type = db_type
        self.output_file = Path(output_file)
        self.source_version = source_version
        self.signals = WorkerSignals()

    def run(self) -> None:  # noqa: C901 (Qt entrypoint)
        from db_project_manager.application.yaml_apply_service import YamlApplyError
        from db_project_manager.infrastructure.yaml_project.generator import (
            generate_yaml_project,
        )
        from db_project_manager.infrastructure.yaml_project.serializer import (
            serialize_yaml_project,
        )

        try:
            self.signals.status.emit(
                f"Сканирование каталога: {self.source_dir} (db_type={self.db_type})"
            )
            project = generate_yaml_project(
                self.source_dir,
                self.db_type,
                source_version=self.source_version,
            )
            yaml_text = serialize_yaml_project(project)
            self.output_file.parent.mkdir(parents=True, exist_ok=True)
            self.output_file.write_text(yaml_text, encoding="utf-8")
            self.signals.status.emit(f"YAML сохранён: {self.output_file}")
            self.signals.finished.emit(self.output_file)
        except YamlApplyError as e:
            self.signals.error.emit(f"YAML generate: {e}")
            self.signals.finished.emit(None)
        except Exception as e:  # noqa: BLE001
            self.signals.error.emit(f"Непредвиденная ошибка: {e}")
            self.signals.finished.emit(None)


class YamlApplyWorker(QRunnable):
    """Run db-pm yaml apply off the UI thread (Phase 13)."""

    def __init__(
        self,
        yaml_file: str | Path,
        target_db_type: str,
        output_dir: str | Path,
        service_schema: str = "__deploy",
    ) -> None:
        super().__init__()
        self.yaml_file = Path(yaml_file)
        self.target_db_type = target_db_type
        self.output_dir = Path(output_dir)
        self._service_schema = service_schema
        self.signals = WorkerSignals()

    def run(self) -> None:  # noqa: C901 (Qt entrypoint)
        from db_project_manager.application.yaml_apply_service import (
            YamlApplyError,
            YamlApplyService,
        )
        from db_project_manager.infrastructure.yaml_project.serializer import (
            parse_yaml_project,
        )

        try:
            self.signals.status.emit(f"Чтение YAML: {self.yaml_file}")
            yaml_text = self.yaml_file.read_text(encoding="utf-8")
            project = parse_yaml_project(yaml_text)
            self.signals.status.emit(
                f"Применение YAML → {self.target_db_type}: {self.output_dir}"
            )
            service = YamlApplyService(service_schema=self._service_schema)
            result = service.run(project, self.output_dir, self.target_db_type)
            self.signals.status.emit(
                f"YAML apply done: schemas={result.schemas_count}, "
                f"objects={result.objects_count}, "
                f"output={result.output_dir}"
            )
            self.signals.finished.emit(result)
        except YamlApplyError as e:
            self.signals.error.emit(f"YAML apply: {e}")
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


class LoadPlanReportWorker(QRunnable):
    """Load + parse a ``plan.json`` off the UI thread (Phase 15).

    Mirrors :class:`LoadDiffReportWorker` (Phase 14) — keeps large-file parsing
    off the UI thread for :class:`~db_project_manager.presentation.gui.widgets.plan_viewer.PlanViewerWindow`.
    Emits the parsed :class:`~db_project_manager.domain.delta.DeltaPlan` on success,
    or ``None`` + an error message on failure (missing/invalid file).
    """

    def __init__(self, path: str | Path) -> None:
        super().__init__()
        self.path = Path(path)
        self.signals = WorkerSignals()

    def run(self) -> None:  # noqa: C901 (Qt entrypoint)
        from db_project_manager.infrastructure.deploy.plan_report import load_plan_report
        from pydantic import ValidationError

        try:
            plan = load_plan_report(self.path)
            self.signals.status.emit(f"План загружен: {self.path.name}")
            self.signals.finished.emit(plan)
        except (ValidationError, ValueError) as e:
            self.signals.error.emit(f"Не удалось разобрать план: {e}")
            self.signals.finished.emit(None)
        except OSError as e:
            self.signals.error.emit(f"Не удалось прочитать файл: {e}")
            self.signals.finished.emit(None)
        except Exception as e:  # noqa: BLE001
            self.signals.error.emit(f"Непредвиденная ошибка: {e}")
            self.signals.finished.emit(None)


class DeployInitServiceSchemaWorker(QRunnable):
    """Run ``db-pm deploy init-service-schema`` (idempotent __deploy bootstrap) off the UI thread.

    Phase 15.5.2 (cis_zup feedback 2026-09-02): on a freshly created target DB
    this MUST be run BEFORE the first ``deploy apply``, because apply silently
    skips ``__deploy`` (CompareService flags it UNCHANGED via RE seeding into
    a temp snapshot). Emits ``ServiceSchemaInitializerResult`` through
    ``finished`` on success; ``ServiceSchemaInitializerError`` and unexpected
    exceptions go via ``signals.error`` with ``finished(None)``.
    """

    def __init__(
        self,
        conn_cfg: ConnectionConfig,
        *,
        service_schema: str = DEFAULT_SERVICE_SCHEMA,
    ) -> None:
        super().__init__()
        self.conn_cfg = conn_cfg
        self.service_schema = service_schema
        self.signals = WorkerSignals()

    def run(self) -> None:
        from db_project_manager.application.service_schema_initializer import (
            ServiceSchemaInitializer,
            ServiceSchemaInitializerError,
        )
        from db_project_manager.infrastructure.config.app_config import load_cfg

        if self.service_schema == DEFAULT_SERVICE_SCHEMA:
            # Pull cfg.deploy.service_schema only when caller didn't pass an
            # explicit value; otherwise honour what the dialog wired through.
            self.service_schema = load_cfg(None).deploy.service_schema

        initializer = ServiceSchemaInitializer(service_schema=self.service_schema)

        def progress(message: str, current: int, total: int) -> None:
            if total:
                self.signals.progress.emit(message, current, total)
            else:
                self.signals.status.emit(message)

        try:
            result = initializer.run(self.conn_cfg, progress=progress)
        except ServiceSchemaInitializerError as e:
            self.signals.error.emit(str(e))
            self.signals.finished.emit(None)
            return
        except Exception as e:  # noqa: BLE001
            self.signals.error.emit(f"Непредвиденная ошибка: {e}")
            self.signals.finished.emit(None)
            return

        # Phase 15.5.2 UX: explicit summary message that distinguishes
        # 'created' from 'no-op' for the GUI result dialog.
        if result.changed:
            summary = (
                f"✓ Init-service-schema: схема {result.service_schema} "
                f"{'создана' if result.created_schema else 'уже была'}, "
                f"таблицы созданы: {', '.join(result.created_tables) or '—'}"
            )
        else:
            summary = (
                f"✓ Init-service-schema: {result.service_schema} + "
                f"{len(result.tables_present)} таблиц уже существуют "
                "(идемпотентный no-op)."
            )
        self.signals.status.emit(summary)
        self.signals.finished.emit(result)

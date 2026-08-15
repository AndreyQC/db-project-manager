"""Safety-gate orchestration for ``deploy analyze`` (Phase 11, S5).

Dry-run pre-analysis against an **existing** target database (the first
real-target path in the project — everything before Phase 11 deployed into a
throwaway temp DB). The service never mutates the target: it connects
read-only in spirit (no ``create/execute/record`` adapter calls are made),
builds the code↔DB delta by reusing :class:`CompareService`, and applies the
safety rules CD-6..CD-10:

    touched (CHANGED/REMOVED table) ∧ has-data (or unknown, fail-safe)
    ∧ no covering pre-script (``project.covers``) → VIOLATION

Pipeline (vision_final §4.1):
    1. adapter via ``get_adapter`` (multi-DB dispatch, SG-M)
    2. manifest: ``db_type`` consistency check + ``source_version``
    3. forward-only version-check (SG-6)
    4. delta via ``CompareService`` (source=DIR code, target=DB live)
    5. touched tables from the diff report
    6. presence stats via ``get_table_presence_stats`` (SG-4/SG-5),
       service schema excluded, fail-safe on missing stats
    7. coverage via ``project.covers`` (SG-3)
    8. verdict → ``safety_gate_report.{md,json}`` (SG-2)
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from loguru import logger

from db_project_manager.application.compare_service import (
    DIFF_REPORT_FILENAME,
    CompareError,
    CompareService,
    SideSpec,
)
from db_project_manager.application.reverse_engineer import ProgressCallback
from db_project_manager.domain.connection import ConnectionConfig
from db_project_manager.domain.diff import DiffReport, DiffStatus, SnapshotSourceKind
from db_project_manager.domain.safety import (
    SafetyGateVerdict,
    StatsConfidence,
    TablePresenceStats,
    TableTouchKind,
    TouchedTable,
    check_version_relation,
    classify_presence,
)
from db_project_manager.infrastructure.config.codebase_manifest import (
    ManifestError,
    read_manifest,
)
from db_project_manager.infrastructure.database.base import DatabaseAdapter, DatabaseError
from db_project_manager.infrastructure.database.registry import get_adapter
from db_project_manager.infrastructure.deploy.canonical_ddl import DEFAULT_SERVICE_SCHEMA
from db_project_manager.infrastructure.deploy.pre_coverage import read_pre_coverage
from db_project_manager.infrastructure.deploy.safety_report import write_safety_report

#: Diff statuses that make a table "touched" by the pending delta. ADDED tables
#: are new (CD-10: always allowed), UNCHANGED are no-ops — neither is modelled.
_TOUCHED_STATUSES = (DiffStatus.CHANGED, DiffStatus.REMOVED)

_STATUS_TO_TOUCH = {
    DiffStatus.CHANGED: TableTouchKind.CHANGED,
    DiffStatus.REMOVED: TableTouchKind.REMOVED,
}

_TOTAL_STEPS = 6


class SafetyGateError(Exception):
    """Hard error of ``deploy analyze`` (exit code 2): connection/manifest/
    version/db-type problems that stop the analysis before a verdict."""


class SafetyGateService:
    """Runs the dry-run safety gate; read-only with respect to the target DB."""

    def __init__(
        self,
        adapter_factory: Callable[[ConnectionConfig], DatabaseAdapter] | None = None,
        compare_service: CompareService | None = None,
        service_schema: str = DEFAULT_SERVICE_SCHEMA,
    ) -> None:
        self._adapter_factory = adapter_factory or get_adapter
        self._compare_service = compare_service or CompareService()
        self._service_schema = service_schema

    def analyze(
        self,
        codebase_dir: str | Path,
        target_cfg: ConnectionConfig,
        output_dir: str | Path,
        progress: ProgressCallback | None = None,
    ) -> SafetyGateVerdict:
        """Run the gate; returns the verdict (reports written to *output_dir*).

        Raises :class:`SafetyGateError` on hard errors (bad manifest, db-type
        mismatch, target newer than source, compare failure) — the CLI maps
        those to exit code 2. A *violation* verdict is a normal return with
        ``clean=False`` (exit code 1).
        """
        codebase_dir = Path(codebase_dir)
        output_dir = Path(output_dir)
        self._emit(progress, "Чтение манифеста кодовой базы…", 0, _TOTAL_STEPS)
        try:
            manifest = read_manifest(codebase_dir)
        except ManifestError as e:
            raise SafetyGateError(str(e)) from e

        # SG-M fail-fast: comparing a codebase against a database of another
        # type is meaningless; CompareService would reject it too, but only
        # after the expensive reverse-engineer of the DB side.
        if manifest.db_type != target_cfg.type:
            raise SafetyGateError(
                f"Тип кодовой базы ({manifest.db_type}) не совпадает с типом "
                f"целевого подключения ({target_cfg.type})."
            )

        self._emit(progress, "Подключение к целевой БД…", 1, _TOTAL_STEPS)
        adapter = self._adapter_factory(target_cfg)
        try:
            adapter.connect(target_cfg)
            return self._analyze_connected(
                adapter, manifest, codebase_dir, target_cfg, output_dir, progress
            )
        finally:
            adapter.disconnect()

    # --- connected phase ---

    def _analyze_connected(
        self,
        adapter: DatabaseAdapter,
        manifest,  # CodebaseManifest (typed loosely to avoid a circular import)
        codebase_dir: Path,
        target_cfg: ConnectionConfig,
        output_dir: Path,
        progress: ProgressCallback | None,
    ) -> SafetyGateVerdict:
        # Forward-only version-check (SG-6). A target without __deploy yet
        # (get_schema_version raising on a missing table) is the normal
        # first-deploy case → None → proceed.
        self._emit(progress, "Проверка версии целевой БД…", 2, _TOTAL_STEPS)
        target_version = self._read_target_version(adapter)
        outcome = check_version_relation(target_version, manifest.source_version)
        if outcome.value == "error_newer":
            raise SafetyGateError(
                f"Целевая БД новее кодовой базы (target={target_version}, "
                f"source={manifest.source_version}). Forward-only: деплой "
                "назад запрещён (ROADMAP §7 п.8)."
            )
        if outcome.value == "warn_same":
            logger.warning(
                f"Версия целевой БД совпадает с source_version "
                f"({target_version}) — возможно, забыли bump в manifest."
            )

        self._emit(progress, "Построение дельты код ↔ БД (compare)…", 3, _TOTAL_STEPS)
        report = self._build_delta(codebase_dir, target_cfg, output_dir, progress)

        self._emit(progress, "Оценка наличия данных…", 4, _TOTAL_STEPS)
        stats = self._presence_lookup(adapter)
        coverage = read_pre_coverage(codebase_dir / "__migrations")

        touched = self._collect_touched(report, stats, coverage, self._service_schema)
        verdict = SafetyGateVerdict(
            clean=not any(t.is_violation for t in touched),
            db_type=manifest.db_type,
            source_version=manifest.source_version,
            target_version=target_version,
            touched=touched,
        )

        self._emit(progress, "Запись отчёта safety-gate…", 5, _TOTAL_STEPS)
        paths = write_safety_report(verdict, output_dir)
        logger.info(
            f"deploy analyze завершён: verdict={'CLEAN' if verdict.clean else 'VIOLATIONS'}, "
            f"touched={len(touched)}, нарушений={len(verdict.violations)}. Отчёты: "
            + ", ".join(str(p) for p in paths)
        )
        return verdict

    def _read_target_version(self, adapter: DatabaseAdapter) -> str | None:
        try:
            return adapter.get_schema_version(self._service_schema)
        except DatabaseError as e:
            logger.warning(
                f"Не удалось прочитать версию целевой БД ({e}) — считаем первым "
                "деплоем (нет __deploy.schema_version)."
            )
            return None

    def _build_delta(
        self,
        codebase_dir: Path,
        target_cfg: ConnectionConfig,
        output_dir: Path,
        progress: ProgressCallback | None,
    ) -> DiffReport:
        """Compare code (DIR) against the live target DB; report stays in output_dir."""
        source = SideSpec(kind=SnapshotSourceKind.DIR, ref=str(codebase_dir))
        target = SideSpec(
            kind=SnapshotSourceKind.DB,
            ref=target_cfg.name or target_cfg.database,
            conn_cfg=target_cfg,
        )
        try:
            self._compare_service.run(source, target, output_dir, progress=progress)
        except CompareError as e:
            raise SafetyGateError(f"Построение дельты не удалось: {e}") from e
        # CompareService.run returns the output dir; the report travels via
        # diff_report.json (round-trip through pydantic, LESSONS §28).
        report_path = output_dir / DIFF_REPORT_FILENAME
        try:
            return DiffReport.model_validate_json(
                report_path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as e:
            raise SafetyGateError(f"Отчёт compare не найден/повреждён: {e}") from e

    def _presence_lookup(
        self, adapter: DatabaseAdapter
    ) -> dict[tuple[str, str], TablePresenceStats]:
        """{(schema, table): stats} for user tables, service schema excluded.

        A DatabaseError here degrades to an empty lookup: every touched table
        then classifies as UNKNOWN (fail-safe — treated as having data).
        """
        try:
            stats = adapter.get_table_presence_stats()
        except DatabaseError as e:
            logger.warning(
                f"Не удалось получить presence stats ({e}) — все тронутые "
                "таблицы классифицируются как UNKNOWN (fail-safe)."
            )
            return {}
        return {
            (s.object_schema, s.name): s
            for s in stats
            if s.object_schema != self._service_schema
        }

    @staticmethod
    def _collect_touched(
        report: DiffReport,
        stats: dict[tuple[str, str], TablePresenceStats],
        coverage: dict[tuple[str, str], list[str]],
        service_schema: str,
    ) -> list[TouchedTable]:
        """DiffReport entries → TouchedTable records (CD-6/CD-7/CD-8).

        Objects of the service schema (``__deploy``) are skipped here — its
        bookkeeping tables are always non-empty, but that is service state,
        not user data (SG-4, application-layer exclusion).
        """
        touched: list[TouchedTable] = []
        for entry in report.entries:
            if entry.status not in _TOUCHED_STATUSES:
                continue
            snap = entry.source_snapshot or entry.target_snapshot
            if snap is None or snap.object_type != "table":
                continue
            if snap.object_schema == service_schema:
                continue
            key = (snap.object_schema, snap.object_name)
            table_stats = stats.get(key) or TablePresenceStats(
                object_schema=key[0], name=key[1],
                estimated_rows=None, confidence=StatsConfidence.UNKNOWN,
            )
            touched.append(
                TouchedTable(
                    object_schema=key[0],
                    name=key[1],
                    touch=_STATUS_TO_TOUCH[entry.status],
                    estimated_rows=table_stats.estimated_rows,
                    confidence=table_stats.confidence,
                    presence=classify_presence(table_stats),
                    covered_by=list(coverage.get(key, [])),
                )
            )
        return touched

    @staticmethod
    def _emit(
        progress: ProgressCallback | None, message: str, current: int, total: int
    ) -> None:
        if progress is not None:
            progress(message, current, total)

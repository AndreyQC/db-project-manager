"""Deploy-apply orchestration (Phase 12, S6; CD-11, CD-14, CD-15; ALT-5/ALT-8).

The first service in the project that **mutates an existing target database**.
Safety comes from two mechanisms (vision_final §3):

1. **Rehearsal (ALT-5):** before touching the live target, the full pipeline runs
   against a temp analog: reverse-engineer the target → deploy its state into a
   fresh temp DB (reusing ``DeployValidateService`` with ``keep_db=True``) → run
   the user's seed scripts (``__migrations/seed/``) → run the whole pipeline there.
   A rehearsal failure leaves the target untouched; the temp DB is dropped unless
   ``--keep-rehearsal-db``.

2. **Conservative classification (ALT-3):** the applied delta may contain only
   SAFE operations. After the pre-scripts, the delta is re-computed (CD-11) and any
   residual NEEDS_PRE/BLOCKED operation stops the pipeline — the semantics being
   that a covering pre-script must bring its tables to a state where the residual
   delta is safe.

Recovery model (no mega-transaction): each delta script executes as its own
statement batch with stop-on-error. A failed apply is re-run, not rolled back: the
fresh compare shortens the delta by whatever already applied, and executed
pre/post scripts are skipped via ``script_history`` (Phase 10 idempotency).

``deploy plan`` (dry-run) shares the same pipeline with ``execute=False``: gate +
fresh delta + artifacts, nothing applied, no pre-scripts run.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger

from db_project_manager.application.compare_service import (
    DIFF_REPORT_FILENAME,
    CompareError,
    CompareService,
    SideSpec,
)
from db_project_manager.application.delta_service import DeltaService
from db_project_manager.application.deploy_service import (
    DeployError,
    DeployPermissionError,
    DeployValidateService,
)
from db_project_manager.application.reverse_engineer import (
    ProgressCallback,
    ReverseEngineerError,
    ReverseEngineerService,
)
from db_project_manager.application.safety_gate_service import (
    SafetyGateError,
    SafetyGateService,
)
from db_project_manager.application.script_runner import (
    ScriptExecutionError,
    ScriptRunner,
)
from db_project_manager.domain.connection import ConnectionConfig
from db_project_manager.domain.delta import DeltaPlan, OperationClass
from db_project_manager.domain.diff import DiffReport, DiffStatus, SnapshotSourceKind
from db_project_manager.domain.safety import (
    DataPresence,
    TablePresenceStats,
    check_version_relation,
    classify_presence,
)
from db_project_manager.infrastructure.config.codebase_manifest import (
    ManifestError,
    read_manifest,
)
from db_project_manager.infrastructure.database.base import DatabaseAdapter, DatabaseError
from db_project_manager.infrastructure.database.registry import get_adapter
from db_project_manager.infrastructure.deploy.alter_plan import classify
from db_project_manager.infrastructure.deploy.canonical_ddl import DEFAULT_SERVICE_SCHEMA
from db_project_manager.infrastructure.deploy.pre_coverage import read_pre_coverage
from db_project_manager.infrastructure.sql.autodoc import strip_autodoc

SEED_DIR_NAME = "__migrations/seed"
REHEARSAL_DIR_NAME = "rehearsal"
REHEARSAL_PREFIX = "dbpm_rehearsal"

#: Diff statuses that make a table "touched" (mirrors the safety gate).
_TOUCHED_STATUSES = (DiffStatus.CHANGED, DiffStatus.REMOVED)


class DeployApplyError(Exception):
    """Hard error of ``deploy apply``/``deploy plan`` (exit code 2): manifest,
    version, db-type, connection, rehearsal-infrastructure problems."""


class DeployApplyRejected(Exception):
    """Safety violation (exit code 1): gate violations or residual non-SAFE
    operations after the pre-scripts (CD-11). The target is not mutated."""


@dataclass
class ApplyResult:
    """Outcome of a successful ``deploy apply``."""

    planned: int                      # operations in the final (target) plan
    applied: int                      # operations actually executed on the target
    applied_version: str | None       # source_version recorded in __deploy
    rehearsal_db: str | None          # temp DB name (None when --no-rehearsal)
    output_dir: Path
    rehearsal_dir: Path | None = None
    artifacts: list[Path] = field(default_factory=list)


class DeployApplyService:
    """Runs ``deploy plan`` (dry-run) and ``deploy apply`` (rehearsal + target)."""

    def __init__(
        self,
        adapter_factory: Callable[[ConnectionConfig], DatabaseAdapter] | None = None,
        compare_service: CompareService | None = None,
        delta_service: DeltaService | None = None,
        deploy_validate: DeployValidateService | None = None,
        reverse_engineer: ReverseEngineerService | None = None,
        service_schema: str = DEFAULT_SERVICE_SCHEMA,
    ) -> None:
        self._adapter_factory = adapter_factory or get_adapter
        self._compare = compare_service or CompareService()
        self._delta = delta_service or DeltaService()
        self._deploy_validate = deploy_validate or DeployValidateService(
            adapter_factory=self._adapter_factory
        )
        self._reverse_engineer = reverse_engineer or ReverseEngineerService()
        self._service_schema = service_schema
        # Injected as a plain object in tests (only .analyze is called).
        self._gate: SafetyGateService | None = None

    # ------------------------------------------------------------------ plan

    def plan(
        self,
        codebase_dir: str | Path,
        target_cfg: ConnectionConfig,
        output_dir: str | Path,
        *,
        include_drops: bool = False,
        progress: ProgressCallback | None = None,
    ) -> DeltaPlan:
        """Dry-run: gate + fresh delta + artifacts. Nothing is executed."""
        return self._run_pipeline(
            target_cfg, codebase_dir, output_dir,
            include_drops=include_drops, execute=False, progress=progress,
        )

    # ----------------------------------------------------------------- apply

    def apply(
        self,
        codebase_dir: str | Path,
        target_cfg: ConnectionConfig,
        output_dir: str | Path,
        *,
        include_drops: bool = False,
        rehearsal: bool = True,
        keep_rehearsal_db: bool = False,
        progress: ProgressCallback | None = None,
    ) -> ApplyResult:
        """Full pipeline; see the module docstring for the safety model."""
        codebase_dir = Path(codebase_dir)
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        rehearsal_db: str | None = None
        if rehearsal:
            rehearsal_db = self._run_rehearsal(
                codebase_dir, target_cfg, output_dir,
                include_drops=include_drops,
                keep_db=keep_rehearsal_db,
                progress=progress,
            )

        self._emit(progress, "Применение дельты к целевой БД…", 0, 0)
        plan = self._run_pipeline(
            target_cfg, codebase_dir, output_dir,
            include_drops=include_drops, execute=True, progress=progress,
        )
        applied = sum(
            1 for op in plan.operations
            if op.script_file and op.classification is OperationClass.SAFE
        )
        result = ApplyResult(
            planned=len(plan.operations),
            applied=applied,
            applied_version=plan.source_version,
            rehearsal_db=rehearsal_db,
            output_dir=output_dir,
            rehearsal_dir=output_dir / REHEARSAL_DIR_NAME if rehearsal_db else None,
        )
        logger.info(
            f"deploy apply завершён: операций {result.applied}/{result.planned}, "
            f"версия {result.applied_version}"
            + (f", репетиция {rehearsal_db}" if rehearsal_db else "")
        )
        return result

    # ------------------------------------------------------------- rehearsal

    def _run_rehearsal(
        self,
        codebase_dir: Path,
        target_cfg: ConnectionConfig,
        output_dir: Path,
        *,
        include_drops: bool,
        keep_db: bool,
        progress: ProgressCallback | None,
    ) -> str:
        """Reproduce the target state in a temp DB, seed it, run the pipeline.

        Returns the rehearsal DB name (kept when *keep_db*, dropped otherwise).
        Any failure raises :class:`DeployApplyError` — the live target is never
        touched by this phase.
        """
        self._emit(progress, "Репетиция: снятие текущего состояния таргета (RE)…", 0, 0)
        temp_root = Path(tempfile.mkdtemp(prefix="dbpm_rehearsal_re_"))
        rehearsal_db: str | None = None
        try:
            try:
                self._reverse_engineer.run(target_cfg, temp_root, progress=progress)
            except ReverseEngineerError as e:
                raise DeployApplyError(f"Репетиция: reverse-engineer таргета не удался: {e}") from e
            re_codebase = temp_root / target_cfg.database

            self._emit(progress, "Репетиция: воспроизведение состояния таргета в temp-БД…", 0, 0)
            try:
                deploy_result = self._deploy_validate.run(
                    target_cfg, re_codebase,
                    prefix=REHEARSAL_PREFIX, keep_db=True, progress=progress,
                )
            except DeployPermissionError as e:
                raise DeployApplyError(
                    f"Репетиция требует права CREATEDB: {e}"
                ) from e
            except DeployError as e:
                raise DeployApplyError(
                    f"Репетиция: не удалось воспроизвести состояние таргета: {e}"
                ) from e
            if not deploy_result.success:
                raise DeployApplyError(
                    "Репетиция: воспроизведение состояния таргета завершилось с ошибками: "
                    + "; ".join(err.error for err in deploy_result.errors)
                )
            rehearsal_db = deploy_result.db_name
            rehearsal_cfg = target_cfg.model_copy(update={"database": rehearsal_db})

            self._run_seed(rehearsal_cfg, codebase_dir, progress)

            self._emit(progress, f"Репетиция: прогон деплоя на аналоге ({rehearsal_db})…", 0, 0)
            try:
                self._run_pipeline(
                    rehearsal_cfg, codebase_dir, output_dir / REHEARSAL_DIR_NAME,
                    include_drops=include_drops, execute=True, progress=progress,
                )
            except DeployApplyRejected as e:
                raise DeployApplyError(
                    f"Репетиция отклонена (таргет не тронут): {e}"
                ) from e
            except DeployApplyError as e:
                raise DeployApplyError(f"Репетиция: {e} (таргет не тронут)") from e
            logger.info(f"Репетиция успешна: {rehearsal_db}")
            return rehearsal_db
        finally:
            if rehearsal_db and not keep_db:
                self._drop_quietly(target_cfg, rehearsal_db)
            shutil.rmtree(temp_root, ignore_errors=True)

    def _run_seed(
        self,
        rehearsal_cfg: ConnectionConfig,
        codebase_dir: Path,
        progress: ProgressCallback | None,
    ) -> None:
        """Execute ``__migrations/seed/*.sql`` against the rehearsal DB only (ALT-8).

        Seeds are never recorded in ``script_history`` — the rehearsal DB is
        disposable and recreated on every apply.
        """
        seed_dir = codebase_dir / SEED_DIR_NAME
        if not seed_dir.is_dir():
            logger.debug(f"Каталог seed не найден ({seed_dir}) — репетиция без данных.")
            return
        scripts = sorted(seed_dir.glob("*.sql"))
        if not scripts:
            logger.info(f"Seed-скриптов нет в {seed_dir} — репетиция без данных.")
            return
        adapter = self._adapter_factory(rehearsal_cfg)
        try:
            adapter.connect(rehearsal_cfg)
            for idx, path in enumerate(scripts, start=1):
                self._emit(progress, f"seed: {path.name}", idx, len(scripts))
                body = strip_autodoc(path.read_text(encoding="utf-8-sig"))
                try:
                    adapter.execute_script(body)
                except DatabaseError as e:
                    raise DeployApplyError(
                        f"Seed-скрипт {path.name} упал на репетиционной БД: {e}"
                    ) from e
        finally:
            adapter.disconnect()

    def _drop_quietly(
        self, target_cfg: ConnectionConfig, db_name: str
    ) -> None:
        adapter = self._adapter_factory(target_cfg)
        try:
            adapter.connect(target_cfg)
            adapter.drop_database(db_name)
        except DatabaseError as e:  # noqa: BLE001 — cleanup must never mask the result
            logger.warning(f"Не удалось удалить репетиционную БД {db_name}: {e}")
        finally:
            adapter.disconnect()

    # --------------------------------------------------------------- pipeline

    def _run_pipeline(
        self,
        conn_cfg: ConnectionConfig,
        codebase_dir: str | Path,
        output_dir: str | Path,
        *,
        include_drops: bool,
        execute: bool,
        progress: ProgressCallback | None,
    ) -> DeltaPlan:
        """Shared core for plan (execute=False) and apply (execute=True).

        Steps (vision_final §4.6): manifest+version-check → gate → pre → fresh
        delta (CD-11) → artifacts → apply → post → record version. In plan mode
        the pre/apply/post/record steps are skipped entirely.
        """
        codebase_dir = Path(codebase_dir)
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        self._emit(progress, "Чтение манифеста кодовой базы…", 0, 0)
        try:
            manifest = read_manifest(codebase_dir)
        except ManifestError as e:
            raise DeployApplyError(str(e)) from e
        if manifest.db_type != conn_cfg.type:
            raise DeployApplyError(
                f"Тип кодовой базы ({manifest.db_type}) не совпадает с типом "
                f"целевого подключения ({conn_cfg.type})."
            )

        adapter = self._adapter_factory(conn_cfg)
        try:
            adapter.connect(conn_cfg)
            target_version = self._read_target_version(adapter)
            outcome = check_version_relation(target_version, manifest.source_version)
            if outcome.value == "error_newer":
                raise DeployApplyError(
                    f"Целевая БД новее кодовой базы (target={target_version}, "
                    f"source={manifest.source_version}). Forward-only: деплой "
                    "назад запрещён (ROADMAP §7 п.8)."
                )
            if outcome.value == "warn_same":
                logger.warning(
                    f"Версия целевой БД совпадает с source_version "
                    f"({target_version}) — возможно, забыли bump в manifest."
                )

            self._emit(progress, "Safety-gate (deploy analyze)…", 0, 0)
            gate = self._gate_service()
            try:
                verdict = gate.analyze(codebase_dir, conn_cfg, output_dir, progress=progress)
            except SafetyGateError as e:
                raise DeployApplyError(str(e)) from e
            if verdict.violations:
                residual = self._gate_residual_violations(
                    verdict, adapter, codebase_dir, output_dir
                )
                if residual and execute:
                    raise DeployApplyRejected(
                        "safety-gate: непокрытые таблицы с данными, дельта по ним "
                        "не классифицируется как safe "
                        f"({len(residual)}): "
                        + "; ".join(f"{t.object_schema}.{t.name}" for t in residual)
                    )
                if residual:
                    # Plan mode: the violations are the point of the review —
                    # they surface as BLOCKED operations in plan.md instead.
                    logger.info(
                        "plan: gate-нарушения попадут в план как BLOCKED-операции "
                        f"({len(residual)})."
                    )

            if execute and manifest.source_version:
                self._emit(progress, "Выполнение pre-скриптов…", 0, 0)
                runner = ScriptRunner(
                    adapter, self._service_schema,
                    deploy_version=manifest.source_version, deploy_source="apply",
                )
                try:
                    runner.run_phase(
                        "pre", codebase_dir / "__migrations",
                        on_progress=lambda m, c, t: self._emit(progress, f"pre: {m}", c, t),
                    )
                except ScriptExecutionError as e:
                    raise DeployApplyError(f"Pre-скрипт упал: {e}") from e

            self._emit(progress, "Повторная дельта (CD-11)…", 0, 0)
            report = self._fresh_compare(codebase_dir, conn_cfg, output_dir)
            stats = self._presence_lookup(adapter)
            coverage = read_pre_coverage(codebase_dir / "__migrations")
            plan = self._delta.build_plan(
                codebase_dir, report, stats, coverage,
                db_type=manifest.db_type,
                source_version=manifest.source_version,
                target_version=target_version,
                include_drops=include_drops,
            )

            if execute:
                unsafe = [
                    op for op in plan.operations
                    if op.classification is not OperationClass.SAFE
                ]
                if unsafe:
                    raise DeployApplyRejected(
                        "CD-11: после pre-скриптов в дельте остались не-safe операции "
                        "(покрывающий pre-скрипт обязан довести таблицы до безопасного "
                        "остатка): "
                        + "; ".join(
                            f"{op.object_schema}.{op.object_name} "
                            f"({op.classification.value}: {op.reason})"
                            for op in unsafe
                        )
                    )

            self._emit(progress, "Запись артефактов дельты…", 0, 0)
            self._delta.write_artifacts(plan, codebase_dir, output_dir)

            if not execute:
                return plan

            self._emit(progress, "Применение дельты…", 0, 0)
            executable = [
                op for op in plan.operations
                if op.script_file and op.classification is OperationClass.SAFE
            ]
            total = len(executable)
            for idx, op in enumerate(executable, start=1):
                self._emit(
                    progress,
                    f"[{idx}/{total}] {op.object_type} {op.object_name}",
                    idx, total,
                )
                script = (output_dir / op.script_file).read_text(encoding="utf-8-sig")
                try:
                    adapter.execute_script(script)
                except DatabaseError as e:
                    raise DeployApplyError(
                        f"Операция {op.object_type} '{op.object_name}' упала; "
                        f"дельта остановлена (stop-on-error, повторный apply "
                        f"пересчитает остаток): {e}"
                    ) from e

            if manifest.source_version:
                self._emit(progress, "Выполнение post-скриптов…", 0, 0)
                runner = ScriptRunner(
                    adapter, self._service_schema,
                    deploy_version=manifest.source_version, deploy_source="apply",
                )
                try:
                    runner.run_phase(
                        "post", codebase_dir / "__migrations",
                        on_progress=lambda m, c, t: self._emit(progress, f"post: {m}", c, t),
                    )
                except ScriptExecutionError as e:
                    raise DeployApplyError(f"Post-скрипт упал: {e}") from e

                self._emit(progress, "Запись schema_version…", 0, 0)
                try:
                    adapter.record_schema_version(
                        self._service_schema, manifest.source_version, "apply"
                    )
                except DatabaseError as e:
                    logger.warning(f"Не удалось записать schema_version: {e}")

            return plan
        finally:
            adapter.disconnect()

    # ---------------------------------------------------------------- helpers

    def _gate_residual_violations(
        self, verdict, adapter: DatabaseAdapter, codebase_dir: Path, output_dir: Path
    ) -> list:
        """Gate violations that survive the Phase 12 column-level classification.

        The Phase 11 gate is table-level (any touched data table without a
        covering pre-script is a violation); ALT-3 additionally allows SAFE
        column changes (e.g. ADD COLUMN nullable) on data tables. This method
        re-classifies each gate violation against the gate's own diff report
        (already written to *output_dir*) and keeps only the tables whose delta
        is genuinely non-safe. A missing/unreadable report is conservative:
        every violation stays.
        """
        try:
            report = DiffReport.model_validate_json(
                (output_dir / DIFF_REPORT_FILENAME).read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as e:
            logger.warning(
                f"Не удалось перечитать отчёт gate для классификации ({e}) — "
                "все gate-нарушения считаются действующими (fail-safe)."
            )
            return list(verdict.violations)

        stats = self._presence_lookup(adapter)
        coverage = read_pre_coverage(codebase_dir / "__migrations")

        residual = []
        for violation in verdict.violations:
            key = (violation.object_schema, violation.name)
            entry = next(
                (
                    e for e in report.entries
                    if e.status in _TOUCHED_STATUSES
                    and (snap := e.source_snapshot or e.target_snapshot) is not None
                    and snap.object_type == "table"
                    and (snap.object_schema, snap.object_name) == key
                ),
                None,
            )
            if entry is None:
                residual.append(violation)
                continue
            table_stats = stats.get(key)
            presence = (
                classify_presence(table_stats) if table_stats is not None
                else DataPresence.UNKNOWN
            )
            op = classify(
                entry, presence, bool(coverage.get(key, [])),
                include_drops=False,
            )
            if op.classification is OperationClass.SAFE:
                logger.info(
                    f"gate: {key[0]}.{key[1]} тронута с данными, но дельта safe "
                    f"({op.reason}) — пропускаем gate-нарушение (ALT-3)."
                )
            else:
                residual.append(violation)
        return residual

    def _gate_service(self):
        """Lazily build the real gate unless a test injected a stub."""
        if self._gate is None:
            self._gate = SafetyGateService(
                adapter_factory=self._adapter_factory,
                compare_service=self._compare,
                service_schema=self._service_schema,
            )
        return self._gate

    def _fresh_compare(
        self, codebase_dir: Path, conn_cfg: ConnectionConfig, output_dir: Path
    ) -> DiffReport:
        source = SideSpec(kind=SnapshotSourceKind.DIR, ref=str(codebase_dir))
        target = SideSpec(
            kind=SnapshotSourceKind.DB,
            ref=conn_cfg.name or conn_cfg.database,
            conn_cfg=conn_cfg,
        )
        try:
            self._compare.run(source, target, output_dir)
        except CompareError as e:
            raise DeployApplyError(f"Построение дельты не удалось: {e}") from e
        report_path = output_dir / DIFF_REPORT_FILENAME
        try:
            return DiffReport.model_validate_json(report_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise DeployApplyError(f"Отчёт compare не найден/повреждён: {e}") from e

    def _presence_lookup(
        self, adapter: DatabaseAdapter
    ) -> dict[tuple[str | None, str], TablePresenceStats]:
        """{(schema, table): stats}; a DatabaseError degrades to {} (fail-safe)."""
        try:
            stats = adapter.get_table_presence_stats()
        except DatabaseError as e:
            logger.warning(
                f"Не удалось получить presence stats ({e}) — все тронутые таблицы "
                "классифицируются как UNKNOWN (fail-safe)."
            )
            return {}
        return {
            (s.object_schema, s.name): s
            for s in stats
            if s.object_schema != self._service_schema
        }

    def _read_target_version(self, adapter: DatabaseAdapter) -> str | None:
        try:
            return adapter.get_schema_version(self._service_schema)
        except DatabaseError as e:
            logger.warning(
                f"Не удалось прочитать версию целевой БД ({e}) — считаем первым "
                "деплоем (нет __deploy.schema_version)."
            )
            return None

    @staticmethod
    def _emit(
        progress: ProgressCallback | None, message: str, current: int, total: int
    ) -> None:
        if progress is not None:
            progress(message, current, total)

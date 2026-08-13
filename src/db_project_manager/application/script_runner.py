"""Pre/post-deploy script runner (Phase 10 S7).

Executes the user-authored SQL scripts that live under
``<codebase>/__migrations/{pre,post}/`` against the ``__deploy`` bookkeeping
tables. The runner is the consumer of the Phase 10 adapter surface (S4) and
of the checksum helpers (S1).

Contract (vision_final §4.4, CDF-1):
  * checksum = script_checksum(strip_autodoc(text)) — executable SQL only.
  * Per script, look up the state row ``get_script_history(name, type)`` —
    exactly one row or None — then decide:
      - None                                  → EXECUTE
      - same checksum AND success             → SKIP    (idempotent)
      - same checksum AND not success         → ERROR   (don't retry a known failure)
      - different checksum                    → ERROR   (CD-4: changed since applied)
  - EXECUTE writes both state (UPSERT) and history (INSERT) atomically via
    ``record_script_execution`` (S4) — success or failure, so a future retry
    has the previous attempt recorded.

Failure modes:
  * default (``continue_on_error=False``) — first failing script raises
    :class:`ScriptExecutionError`; later scripts do NOT run.
  * ``continue_on_error=True`` — every script runs; returned list carries
    success and failure records; caller decides what to do.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from loguru import logger

from db_project_manager.domain.deploy import ScriptRecord, script_checksum
from db_project_manager.infrastructure.database.base import DatabaseAdapter, DatabaseError
from db_project_manager.infrastructure.sql.autodoc import strip_autodoc

#: Filename convention (CDF-3, vision_final §4.3): YYYY-MM-DD_NNN_description.sql.
SCRIPT_NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{3}_.+\.sql$")

#: Progress callback signature: (message, current_step, total_steps).
ProgressCallback = Callable[[str, int, int], None]

Phase = Literal["pre", "post"]


class ScriptExecutionError(Exception):
    """Raised when a pre/post script fails and ``continue_on_error=False``.

    Carries the failing :class:`ScriptRecord` so the caller (deploy service)
    can include it in the deploy result and progress log.
    """

    def __init__(self, record: ScriptRecord) -> None:
        self.record = record
        super().__init__(
            f"{record.script_type}-script {record.script_name!r} failed: "
            f"{record.error_message or '(no detail)'}"
        )


class ScriptRunner:
    """Idempotent runner for pre/post-deploy scripts.

    Stateless beyond the injected adapter — safe to instantiate once per
    deploy. ``deploy_version`` / ``deploy_source`` propagate into the audit
    log so reports (Phase 13 CD-17) can JOIN executions to a specific deploy.
    """

    def __init__(
        self,
        adapter: DatabaseAdapter,
        schema_name: str,
        *,
        deploy_version: str,
        deploy_source: str,
    ) -> None:
        self._adapter = adapter
        self._schema_name = schema_name
        self._deploy_version = deploy_version
        self._deploy_source = deploy_source

    def run_phase(
        self,
        phase: Phase,
        migrations_dir: Path,
        *,
        continue_on_error: bool = False,
        on_progress: ProgressCallback | None = None,
    ) -> list[ScriptRecord]:
        """Execute all scripts of one phase (``pre`` or ``post``), in name order.

        Returns one :class:`ScriptRecord` per script that was attempted
        (EXECUTE) — skipped scripts are NOT in the list (they didn't run, no
        new audit row). Failed scripts are included when
        ``continue_on_error=True``; on ``False`` (default) the first failure
        raises :class:`ScriptExecutionError` and the return value is the
        partial list (but the caller usually only sees the exception).

        Empty / missing ``migrations_dir / phase`` directory → ``[]`` (no-op).
        """
        phase_dir = Path(migrations_dir) / phase
        if not phase_dir.is_dir():
            logger.debug(f"Каталог {phase_dir} не найден — фаза '{phase}' пропущена.")
            return []

        scripts = sorted(phase_dir.glob("*.sql"))
        if not scripts:
            logger.info(f"Нет .sql файлов в {phase_dir} — фаза '{phase}' пропущена.")
            return []

        records: list[ScriptRecord] = []
        total = len(scripts)
        for idx, path in enumerate(scripts, start=1):
            self._emit(on_progress, f"{phase}: {path.name}", idx, total)
            if not SCRIPT_NAME_RE.match(path.name):
                # CDF-3: warn, but still process — legacy scripts are not fatal.
                logger.warning(
                    f"Имя файла {path.name!r} не соответствует конвенции "
                    f"YYYY-MM-DD_NNN_description.sql — выполняется с предупреждением."
                )

            text = path.read_text(encoding="utf-8-sig")
            checksum = script_checksum(strip_autodoc(text))
            existing = self._adapter.get_script_history(
                self._schema_name, path.name, phase
            )

            decision = self._decide(existing, checksum)
            logger.debug(f"{phase}/{path.name}: {decision}")

            if decision == "skip":
                # Idempotent — no new audit row, no record in the result list.
                continue

            if decision == "error":
                # Ambiguous or known-failed state — DO NOT execute; record a failure.
                err = self._describe_error_decision(existing, checksum)
                logger.error(f"{phase}/{path.name}: {err}")
                failure = ScriptRecord(
                    script_name=path.name,
                    script_type=phase,
                    checksum=checksum,
                    success=False,
                    error_message=err,
                    duration_ms=0,
                )
                self._adapter.record_script_execution(
                    self._schema_name, failure,
                    self._deploy_version, self._deploy_source,
                )
                if continue_on_error:
                    records.append(failure)
                    continue
                raise ScriptExecutionError(failure)

            # decision == "execute"
            record = self._execute(path, phase, text, checksum)
            self._adapter.record_script_execution(
                self._schema_name, record,
                self._deploy_version, self._deploy_source,
            )
            if record.success:
                records.append(record)
            elif continue_on_error:
                records.append(record)
            else:
                raise ScriptExecutionError(record)

        return records

    # --- helpers ---

    @staticmethod
    def _decide(existing: ScriptRecord | None, checksum: str) -> Literal["execute", "skip", "error"]:
        """Four-way decision table for skip/error/execute (vision_final §4.4).

        Two axes: content match (same/different checksum) × last outcome
        (success/failure). Two cases re-execute by design:

          * new (existing is None) → EXECUTE;
          * failed + changed checksum → EXECUTE  (retry with fixed content;
            same name is allowed to recover from a known failure).

        Two cases refuse to run:

          * same checksum + success → SKIP (idempotent);
          * same checksum + failure → ERROR (don't blindly retry the same
            failing content);
          * different checksum + success → ERROR (changed since a successful
            apply — ambiguous; use a new filename for a new version, CD-4).
        """
        if existing is None:
            return "execute"
        if existing.checksum == checksum:
            return "skip" if existing.success else "error"
        # Different checksum.
        if not existing.success:
            # Previous attempt failed with different content → allow retry.
            return "execute"
        return "error"

    @staticmethod
    def _describe_error_decision(existing: ScriptRecord | None, checksum: str) -> str:
        if existing is None:
            return "не должно случаться (decision=error при existing=None)"
        if existing.checksum == checksum and not existing.success:
            return (
                "тот же checksum уже падал ранее (success=False). "
                "Разберитесь с причиной и перезапустите; либо измените скрипт."
            )
        # different checksum + success
        return (
            f"скрипт изменился после успешного применения (записан checksum "
            f"{existing.checksum[:8]}…, текущий {checksum[:8]}…). "
            f"Используйте новое имя файла для новой версии скрипта."
        )

    def _execute(
        self, path: Path, phase: Phase, text: str, checksum: str
    ) -> ScriptRecord:
        """Run one script and time it; never raises — failure is captured in the record."""
        body = strip_autodoc(text)
        start = time.perf_counter()
        try:
            self._adapter.execute_script(body)
            duration_ms = int((time.perf_counter() - start) * 1000)
            logger.info(f"{phase}/{path.name} выполнен за {duration_ms} мс")
            return ScriptRecord(
                script_name=path.name,
                script_type=phase,
                checksum=checksum,
                success=True,
                duration_ms=duration_ms,
            )
        except DatabaseError as e:
            duration_ms = int((time.perf_counter() - start) * 1000)
            logger.error(f"{phase}/{path.name} упал: {e}")
            return ScriptRecord(
                script_name=path.name,
                script_type=phase,
                checksum=checksum,
                success=False,
                error_message=str(e),
                duration_ms=duration_ms,
            )

    @staticmethod
    def _emit(
        progress: ProgressCallback | None, message: str, current: int, total: int
    ) -> None:
        if progress is not None:
            progress(message, current, total)

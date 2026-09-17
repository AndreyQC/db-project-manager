"""Schema-reset orchestration for ``deploy reset`` (Phase 18).

Wipes the user schemas of an EXISTING target database so that the next
``deploy plan``/``apply`` runs as a first deploy: every object is ADDED, the
safety gate is CLEAN by construction (CD-10), no ``--include-drops`` and no
covering pre-scripts are needed. Motivated by dev/test databases that
accumulate drift faster than it can be migrated.

Safety model (final doc ``20260917_001_deploy_reset_final.md``):

* **Connection flag (main guard, D3/D6):** the command runs only against
  connections with ``allow_drop_schemas: true``. Checked before connecting
  (rejected fast) and re-checked in :meth:`SchemaResetService.execute`
  (defense in depth).
* **Scope (D1):** all non-service schemas of the target — both codebase
  schemas and accumulated "junk". Never touched: the service schema
  (``__deploy``), system schemas and GP admin schemas (adapter-side guard).
* **Mechanics (D9):** codebase schemas and ``public`` get a content-drop —
  objects go, the schema shell keeps its ACLs/owner/default privileges and the
  next deploy sees it UNCHANGED. Junk schemas absent from the codebase are
  dropped entirely (else their shells would return as REMOVED in the diff).
  An ACL insurance snapshot (``reset_acl_snapshot.sql``) is rendered BEFORE
  any mutation, dry-run included.
* **Journal (D2):** the service schema survives with ``schema_version`` kept
  (forward-only not reset), but ``script_history``/``script_audit_log`` are
  TRUNCATEd so pre/post/seed scripts re-run on the clean database.
* **Multi-DB (D11):** the service contains no SQL and no dialect knowledge —
  every operation goes through the :class:`DatabaseAdapter` ABC; the db_type
  check mirrors the safety gate's SG-M fail-fast.

The CLI/GUI own the human confirmation (typing the database name); the
service receives an already-confirmed call via :meth:`execute`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger

from db_project_manager.application.reverse_engineer import ProgressCallback
from db_project_manager.domain.connection import ConnectionConfig
from db_project_manager.infrastructure.config.codebase_manifest import (
    ManifestError,
    read_manifest,
)
from db_project_manager.infrastructure.database.base import DatabaseAdapter, DatabaseError
from db_project_manager.infrastructure.database.registry import get_adapter
from db_project_manager.infrastructure.deploy.canonical_ddl import DEFAULT_SERVICE_SCHEMA
from db_project_manager.infrastructure.deploy.reset_report import (
    ACL_SNAPSHOT_NAME,
    write_reset_report,
)

#: Top-level codebase directories that are not schemas.
_NON_SCHEMA_DIRS = {"__migrations", "settings"}

#: The schema shell kept in every content-drop regardless of codebase presence.
_ALWAYS_CONTENT_DROP = "public"


class SchemaResetRejected(Exception):
    """The reset was refused (exit code 1): connection flag missing."""


class SchemaResetError(Exception):
    """Hard error of the reset (exit code 2): manifest/db-type/connection/DDL."""


@dataclass
class ResetPlan:
    """Collected pre-flight state (no mutations happened yet).

    Built by :meth:`SchemaResetService.collect`; printed by the CLI for the
    human confirmation, then handed to :meth:`SchemaResetService.execute`.
    """

    target: ConnectionConfig
    db_type: str
    source_version: str | None
    codebase_dir: Path
    service_schema: str
    #: Schemas present in the codebase (top-level dirs minus non-schema dirs).
    in_codebase: set[str] = field(default_factory=set)
    #: All victim schemas of the target DB (user schemas minus service schema).
    schemas: list[str] = field(default_factory=list)
    #: Advisory object counts per schema (confirmation aid).
    object_counts: dict[str, int] = field(default_factory=dict)
    #: Extensions with the schema their objects live in.
    extensions: list[dict] = field(default_factory=list)
    #: Rendered ACL insurance SQL (dialect of the target adapter).
    acl_sql: str = ""

    def is_content_drop(self, schema: str) -> bool:
        """D9 rule: codebase schemas and public keep their shell; junk goes."""
        return schema == _ALWAYS_CONTENT_DROP or schema in self.in_codebase

    def content_drop_schemas(self) -> list[str]:
        return [s for s in self.schemas if self.is_content_drop(s)]

    def full_drop_schemas(self) -> list[str]:
        return [s for s in self.schemas if not self.is_content_drop(s)]


@dataclass
class ResetResult:
    """Outcome of a reset (dry-run included)."""

    target: str
    codebase_dir: Path
    db_type: str
    source_version: str | None
    service_schema: str
    dry_run: bool
    schemas_wiped: list[str] = field(default_factory=list)   # content-drop
    schemas_dropped: list[str] = field(default_factory=list)  # full DROP (junk)
    object_counts: dict[str, int] = field(default_factory=dict)
    extensions_dropped: list[str] = field(default_factory=list)
    journal_truncated: bool = False
    in_codebase: set[str] = field(default_factory=set)
    acl_snapshot_path: Path | None = None
    report_paths: list[Path] = field(default_factory=list)


def codebase_schemas(codebase_dir: str | Path, service_schema: str) -> set[str]:
    """Schema names present in the codebase (cheap directory scan).

    Top-level directories minus ``__migrations``/``settings``, the service
    schema and hidden dirs — ``.dbm_graph`` (graph store, rebuilt by
    ``db-pm graph build``) leaked into ``in_codebase`` on the first live run
    (2026-09-17). The dependency graph is deliberately NOT built — the reset
    needs only names, and the manifest (for the db_type check) is read
    separately.
    """
    root = Path(codebase_dir)
    skip = _NON_SCHEMA_DIRS | {service_schema}
    if not root.is_dir():
        raise SchemaResetError(f"Каталог кодовой базы не найден: {root}")
    return {
        p.name
        for p in root.iterdir()
        if p.is_dir() and not p.name.startswith(".") and p.name not in skip
    }


class SchemaResetService:
    """Two-phase reset: :meth:`collect` (read-only) + :meth:`execute`."""

    def __init__(
        self,
        adapter_factory: Callable[[ConnectionConfig], DatabaseAdapter] | None = None,
        *,
        service_schema: str = DEFAULT_SERVICE_SCHEMA,
    ) -> None:
        self._adapter_factory = adapter_factory or get_adapter
        self._service_schema = service_schema

    # ---------------------------------------------------------------- collect

    def collect(self, codebase_dir: str | Path, target_cfg: ConnectionConfig) -> ResetPlan:
        """Read-only pre-flight: manifest check, flag gate, target inventory.

        Raises :class:`SchemaResetRejected` when the connection lacks the
        ``allow_drop_schemas`` flag (before connecting — fast, no I/O) and
        :class:`SchemaResetError` on manifest/db-type/connection problems.
        """
        codebase_dir = Path(codebase_dir)
        try:
            manifest = read_manifest(codebase_dir)
        except ManifestError as e:
            raise SchemaResetError(str(e)) from e

        # SG-M fail-fast (mirrors SafetyGateService): resetting a codebase
        # against a database of another type is a usage error.
        if manifest.db_type != target_cfg.type:
            raise SchemaResetError(
                f"Тип кодовой базы ({manifest.db_type}) не совпадает с типом "
                f"целевого подключения ({target_cfg.type})."
            )

        # Main guard (D3/D6): refuse before any connection is opened.
        if not target_cfg.allow_drop_schemas:
            raise SchemaResetRejected(
                f"Подключение '{target_cfg.name or target_cfg.database}' не разрешает "
                "сброс схем. Установите allow_drop_schemas: true в YAML подключения "
                "(чекбокс «Разрешить drop-схем» в диалоге подключения GUI)."
            )

        in_codebase = codebase_schemas(codebase_dir, self._service_schema)

        adapter = self._adapter_factory(target_cfg)
        try:
            adapter.connect(target_cfg)
            schemas = [s for s in adapter.list_schemas() if s != self._service_schema]
            object_counts = adapter.get_schema_object_counts()
            extensions = adapter.list_extensions()
            acl_sql = self._snapshot_acls(adapter, schemas)
        except DatabaseError as e:
            raise SchemaResetError(str(e)) from e
        finally:
            adapter.disconnect()

        return ResetPlan(
            target=target_cfg,
            db_type=manifest.db_type,
            source_version=manifest.source_version,
            codebase_dir=codebase_dir,
            service_schema=self._service_schema,
            in_codebase=in_codebase,
            schemas=schemas,
            object_counts={k: v for k, v in object_counts.items() if k in set(schemas)},
            extensions=extensions,
            acl_sql=acl_sql,
        )

    # ---------------------------------------------------------------- execute

    def execute(
        self,
        plan: ResetPlan,
        output_dir: str | Path,
        *,
        dry_run: bool = False,
        progress: ProgressCallback | None = None,
    ) -> ResetResult:
        """Run the wipe according to ``plan``; write artifacts to ``output_dir``.

        Defense in depth: the connection flag is re-checked here even though
        :meth:`collect` already gated on it. Raises :class:`SchemaResetError`
        on the first DDL failure (stop-on-error; the report still describes
        what got dropped).
        """
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        if not plan.target.allow_drop_schemas:
            raise SchemaResetRejected("Флаг allow_drop_schemas сброшен между collect и execute.")

        result = ResetResult(
            target=plan.target.name or plan.target.database,
            codebase_dir=plan.codebase_dir,
            db_type=plan.db_type,
            source_version=plan.source_version,
            service_schema=plan.service_schema,
            dry_run=dry_run,
            object_counts=dict(plan.object_counts),
            in_codebase=set(plan.in_codebase),
        )

        # ACL insurance artifact goes first — before ANY mutation, dry-run too.
        snapshot_path = output_dir / ACL_SNAPSHOT_NAME
        snapshot_path.write_text(plan.acl_sql, encoding="utf-8")
        result.acl_snapshot_path = snapshot_path

        if not dry_run:
            self._emit(progress, "Подключение к целевой БД…", 0, 4)
            adapter = self._adapter_factory(plan.target)
            try:
                adapter.connect(plan.target)
                self._wipe(adapter, plan, result, progress)
            except DatabaseError as e:
                # Stop-on-error: the first failing DDL aborts the wipe; the
                # CLI maps this to exit code 2 with the run-dir artifacts.
                raise SchemaResetError(str(e)) from e
            finally:
                adapter.disconnect()

        result.report_paths = write_reset_report(result, output_dir)
        logger.info(
            f"deploy reset ({'dry-run' if dry_run else 'выполнен'}): "
            f"content-drop {len(result.schemas_wiped)}, полных DROP "
            f"{len(result.schemas_dropped)}, extensions {len(result.extensions_dropped)}. "
            f"Отчёты: {', '.join(str(p) for p in result.report_paths)}"
        )
        return result

    # ------------------------------------------------------------------ wipe

    def _wipe(
        self,
        adapter: DatabaseAdapter,
        plan: ResetPlan,
        result: ResetResult,
        progress: ProgressCallback | None,
    ) -> None:
        self._emit(progress, "Удаление extensions в сбрасываемых схемах…", 1, 4)
        victims = set(plan.schemas)
        for ext in plan.extensions:
            if ext.get("schema") in victims:
                adapter.drop_extension(ext["name"])
                result.extensions_dropped.append(ext["name"])

        self._emit(progress, "Content-drop схем из кодовой базы…", 2, 4)
        for schema in plan.schemas:
            if plan.is_content_drop(schema):
                adapter.drop_schema_contents(schema)
                result.schemas_wiped.append(schema)

        self._emit(progress, "Полный DROP мусорных схем…", 3, 4)
        for schema in plan.schemas:
            if not plan.is_content_drop(schema):
                adapter.drop_schema(schema)
                result.schemas_dropped.append(schema)

        self._emit(progress, "Очистка deploy-журнала…", 4, 4)
        try:
            adapter.truncate_table(plan.service_schema, "script_history")
            adapter.truncate_table(plan.service_schema, "script_audit_log")
            result.journal_truncated = True
        except DatabaseError as e:
            # Missing __deploy is a legal state (first deploy never ran) — the
            # next deploy apply creates it via ServiceSchemaInitializer.
            logger.warning(f"Журнал __deploy не очищен: {e}")

    def _snapshot_acls(self, adapter: DatabaseAdapter, schemas: list[str]) -> str:
        try:
            return adapter.snapshot_schema_acls(schemas)
        except DatabaseError as e:
            # The snapshot is insurance, not a precondition: a failed read
            # degrades to a comment-only artifact, the reset is not blocked.
            logger.warning(f"ACL-снапшот не построен ({e}) — артефакт будет пустым.")
            return f"-- ACL snapshot failed: {e}\n"

    @staticmethod
    def _emit(
        progress: ProgressCallback | None, message: str, current: int, total: int
    ) -> None:
        if progress is not None:
            progress(message, current, total)

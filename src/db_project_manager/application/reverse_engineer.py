"""Reverse-engineering application service.

Connects to a database via an adapter, reads the full structure, and writes
SQL files via the generator. Both CLI and GUI depend on this single entry
point — no business logic lives in the presentation layer.

A progress callback lets the caller observe stages (used by the GUI progress
bar and optional CLI verbose output).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from db_project_manager.domain.connection import ConnectionConfig
from db_project_manager.domain.deploy import calver_seed
from db_project_manager.domain.diff import CodebaseManifest
from db_project_manager.infrastructure.config.codebase_manifest import tool_version, write_manifest
from db_project_manager.infrastructure.database.base import DatabaseAdapter, DatabaseError
from db_project_manager.infrastructure.database.registry import get_adapter
from db_project_manager.infrastructure.deploy.canonical_ddl import (
    DEFAULT_SERVICE_SCHEMA,
    canonical_deploy_ddl,
)
from db_project_manager.infrastructure.sql.autodoc import ensure_header, update_header
from db_project_manager.infrastructure.sql.sql_generator import SQLGenerator

if TYPE_CHECKING:
    from db_project_manager.application.qualify_refs_service import QualifyRefsService

#: Progress callback signature: (stage_message, current_step, total_steps).
ProgressCallback = Callable[[str, int, int], None]


class ReverseEngineerError(Exception):
    """Raised when reverse engineering fails."""


class ReverseEngineerService:
    """Orchestrates adapter + generator for the DB -> files flow."""

    def __init__(
        self,
        generator: SQLGenerator | None = None,
        adapter_factory: Callable[[ConnectionConfig], DatabaseAdapter] | None = None,
        qualify_refs_service: QualifyRefsService | None = None,
        *,
        service_schema: str = DEFAULT_SERVICE_SCHEMA,
    ) -> None:
        self.generator = generator or SQLGenerator()
        # Default factory picks the adapter from ConnectionConfig.type.
        self._adapter_factory = adapter_factory or get_adapter
        # Phase 6: post-process generated files to qualify bare refs.
        # None disables the step (used by tests / when caller wants raw output).
        self._qualify_service = qualify_refs_service
        # Phase 10 S6: configurable service-schema name for seed/sync.
        self._service_schema = service_schema

    def run(
        self,
        conn_cfg: ConnectionConfig,
        output_dir: str | Path,
        progress: ProgressCallback | None = None,
    ) -> Path:
        """Run reverse engineering and return the generated tree root.

        Args:
            conn_cfg: Connection parameters.
            output_dir: Root directory for generated files. A per-database
                subdirectory is created underneath.
            progress: Optional callback receiving (message, current, total).
        """
        output_dir = Path(output_dir)
        adapter = self._adapter_factory(conn_cfg)

        try:
            total = 5 if self._qualify_service is not None else 4
            self._emit(progress, "Подключение к базе данных...", 0, total)
            adapter.connect(conn_cfg)

            self._emit(progress, "Получение структуры базы данных...", 1, total)
            structure = adapter.get_database_structure()

            target = output_dir / conn_cfg.database
            self._emit(progress, f"Генерация SQL-скриптов в: {target}", 2, total)
            result = self.generator.generate_scripts(structure, target, object_catalog=conn_cfg.database)

            if self._qualify_service is not None:
                self._emit(progress, "Квалификация ссылок...", 3, total)
                try:
                    self._qualify_service.run(result)
                except Exception as e:  # noqa: BLE001
                    # Qualify-refs is a best-effort post-processor: never let it
                    # fail the whole reverse-engineer. The report file is still
                    # useful and the user can re-run db-pm qualify-refs manually.
                    logger.warning(f"Qualify-refs пропущен из-за ошибки: {e}")

            # Phase 10 S6: seed/sync the __deploy service schema in the codebase.
            # Closes the DB <-> codebase cycle (vision_final §4.7):
            #   [1] DB without __deploy  → seed 3 tables from canonical templates
            #                              + manifest source_version = calver_seed
            #   [3] DB with __deploy     → already rendered by generator; we just
            #                              mark immutable + sync source_version
            #                              from __deploy.schema_version
            source_version = self._seed_or_sync_deploy(
                structure=structure,
                result=result,
                adapter=adapter,
                object_catalog=conn_cfg.database,
            )

            # Phase 9: write a whole-DB manifest next to the generated tree so the
            # compare feature can read the source db_type without a live connection.
            # Phase 10 (CDF-2): source_version is required at v2 — seeded or synced
            # above (no longer calver_seed() unconditionally).
            manifest = CodebaseManifest(
                db_type=conn_cfg.type,
                database=conn_cfg.database,
                generated_at=datetime.now(timezone.utc).isoformat(),
                tool_version=tool_version(),
                source_version=source_version,
            )
            write_manifest(manifest, result)

            self._emit(progress, "Готово", total, total)
            logger.info(f"Reverse-engineer завершён: {result}")
            return result
        except DatabaseError as e:
            raise ReverseEngineerError(str(e)) from e
        finally:
            adapter.disconnect()

    def _seed_or_sync_deploy(
        self,
        *,
        structure: dict[str, Any],
        result: Path,
        adapter: DatabaseAdapter,
        object_catalog: str,
    ) -> str:
        """Phase 10 S6: seed or sync the service schema in the codebase.

        Returns the ``source_version`` to write into the manifest:
          * DB without __deploy → ``calver_seed()`` (or existing manifest value
            on re-RE without __deploy).
          * DB with __deploy + non-empty schema_version → that version (sync).
          * DB with __deploy + empty schema_version → ``calver_seed()`` + warning.

        Side effects on the codebase (always idempotent):
          * If __deploy is missing → seed schema.sql + tables/{3}.sql from the
            canonical templates with ``immutable: true`` in autodoc.
          * All ``<service_schema>/**/*.sql`` get ``immutable: true`` set in
            their autodoc ``project`` section (whether seeded just now or
            rendered by the generator from a DB that already had __deploy).
        """
        # Need TYPE_CHECKING-safe import here to avoid a module-level cycle.
        from db_project_manager.infrastructure.config.codebase_manifest import (
            MANIFEST_FILENAME,
        )
        from db_project_manager.infrastructure.config.codebase_manifest import read_manifest
        from db_project_manager.infrastructure.config.codebase_manifest import (
            ManifestError,
        )

        service_schema = self._service_schema
        schemas_in_db = {s.get("name") for s in structure.get("schemas") or []}
        has_deploy_in_db = service_schema in schemas_in_db
        deploy_dir = result / service_schema

        if not has_deploy_in_db:
            self._seed_deploy_files(deploy_dir, service_schema, object_catalog)

        # Mark every file in the service schema as immutable (idempotent).
        if deploy_dir.is_dir():
            self._mark_immutable(deploy_dir)

        # Resolve source_version.
        if has_deploy_in_db:
            try:
                current = adapter.get_schema_version(service_schema)
            except Exception as e:  # noqa: BLE001 — best-effort sync; never fatal.
                logger.warning(f"Не удалось прочитать schema_version из БД: {e}")
                current = None
            if current:
                logger.info(f"Sync source_version из __deploy: {current}")
                return current
            logger.warning(
                f"Схема {service_schema} есть в БД, но schema_version пуста — "
                f"seed source_version = calver_seed()."
            )
            return calver_seed()

        # No __deploy in DB. Preserve existing manifest source_version if re-RE.
        manifest_path = result / MANIFEST_FILENAME
        if manifest_path.is_file():
            try:
                existing = read_manifest(result)
                if existing.source_version:
                    return existing.source_version
            except ManifestError as e:
                logger.warning(f"Существующий манифест невалиден, seed calver: {e}")
        return calver_seed()

    def _seed_deploy_files(self, deploy_dir: Path, service_schema: str, db_name: str) -> None:
        """Write the canonical service-schema tree (schema + 3 tables).

        Bypasses SQLGenerator: the canonical templates are the source of truth
        (S5), and rendering them directly ensures the seeded DDL matches what
        ``validate_deploy_ddl`` expects (zero warnings on first RE).
        """
        deploy_dir.mkdir(parents=True, exist_ok=True)
        # schema.sql — minimal CREATE SCHEMA IF NOT EXISTS.
        schema_body = f'CREATE SCHEMA IF NOT EXISTS "{service_schema}";\n'
        (deploy_dir / f"schema {service_schema}.sql").write_text(
            ensure_header(
                schema_body,
                object_catalog=db_name,
                object_schema=service_schema,
                object_type="schema",
                object_name=service_schema,
                immutable=True,
            ),
            encoding="utf-8",
        )
        # tables/*.sql — render canonical DDL, decorate with immutable marker.
        tables_dir = deploy_dir / "tables"
        tables_dir.mkdir(parents=True, exist_ok=True)
        for table_name, body in canonical_deploy_ddl(service_schema).items():
            (tables_dir / f"{table_name}.sql").write_text(
                ensure_header(
                    body,
                    object_catalog=db_name,
                    object_schema=service_schema,
                    object_type="table",
                    object_name=table_name,
                    immutable=True,
                ),
                encoding="utf-8",
            )
        logger.info(
            f"Seed {service_schema}/ (schema + 3 tables) — canonical DDL из встроенных шаблонов."
        )

    @staticmethod
    def _mark_immutable(deploy_dir: Path) -> None:
        """Set ``project.immutable = true`` in the autodoc of every file under deploy_dir.

        Idempotent: ``update_header`` parses → mutator → re-renders, no-op on
        files without autodoc. Safe to run over both freshly-seeded files
        (already carry immutable=True) and generator-rendered ones (carrying
        only build=true).
        """

        def _add_immutable(metadata: dict[str, Any]) -> None:
            project = metadata.setdefault("project", {})
            project["immutable"] = True

        for sql_file in deploy_dir.rglob("*.sql"):
            content = sql_file.read_text(encoding="utf-8")
            updated = update_header(content, _add_immutable)
            if updated != content:
                sql_file.write_text(updated, encoding="utf-8")

    @staticmethod
    def _emit(progress: ProgressCallback | None, message: str, current: int, total: int) -> None:
        if progress is not None:
            progress(message, current, total)


def build_default_service() -> ReverseEngineerService:
    """Convenience constructor for CLI/GUI — wires the default qualify-refs step."""
    # Local import to avoid a cycle (qualify_refs_service imports graph_service).
    from db_project_manager.application.qualify_refs_service import QualifyRefsService

    return ReverseEngineerService(qualify_refs_service=QualifyRefsService())

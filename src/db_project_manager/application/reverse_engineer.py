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
from typing import TYPE_CHECKING

from loguru import logger

from db_project_manager.domain.connection import ConnectionConfig
from db_project_manager.domain.diff import CodebaseManifest
from db_project_manager.infrastructure.config.codebase_manifest import tool_version, write_manifest
from db_project_manager.infrastructure.database.base import DatabaseAdapter, DatabaseError
from db_project_manager.infrastructure.database.registry import get_adapter
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
    ) -> None:
        self.generator = generator or SQLGenerator()
        # Default factory picks the adapter from ConnectionConfig.type.
        self._adapter_factory = adapter_factory or get_adapter
        # Phase 6: post-process generated files to qualify bare refs.
        # None disables the step (used by tests / when caller wants raw output).
        self._qualify_service = qualify_refs_service

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

            # Phase 9: write a whole-DB manifest next to the generated tree so the
            # compare feature can read the source db_type without a live connection.
            manifest = CodebaseManifest(
                db_type=conn_cfg.type,
                database=conn_cfg.database,
                generated_at=datetime.now(timezone.utc).isoformat(),
                tool_version=tool_version(),
            )
            write_manifest(manifest, result)

            self._emit(progress, "Готово", total, total)
            logger.info(f"Reverse-engineer завершён: {result}")
            return result
        except DatabaseError as e:
            raise ReverseEngineerError(str(e)) from e
        finally:
            adapter.disconnect()

    @staticmethod
    def _emit(progress: ProgressCallback | None, message: str, current: int, total: int) -> None:
        if progress is not None:
            progress(message, current, total)


def build_default_service() -> ReverseEngineerService:
    """Convenience constructor for CLI/GUI — wires the default qualify-refs step."""
    # Local import to avoid a cycle (qualify_refs_service imports graph_service).
    from db_project_manager.application.qualify_refs_service import QualifyRefsService

    return ReverseEngineerService(qualify_refs_service=QualifyRefsService())

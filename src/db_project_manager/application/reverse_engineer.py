"""Reverse-engineering application service.

Connects to a database via an adapter, reads the full structure, and writes
SQL files via the generator. Both CLI and GUI depend on this single entry
point — no business logic lives in the presentation layer.

A progress callback lets the caller observe stages (used by the GUI progress
bar and optional CLI verbose output).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from loguru import logger

from db_project_manager.domain.connection import ConnectionConfig
from db_project_manager.infrastructure.database.base import DatabaseAdapter, DatabaseError
from db_project_manager.infrastructure.database.registry import get_adapter
from db_project_manager.infrastructure.sql.sql_generator import SQLGenerator

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
    ) -> None:
        self.generator = generator or SQLGenerator()
        # Default factory picks the adapter from ConnectionConfig.type.
        self._adapter_factory = adapter_factory or get_adapter

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
            self._emit(progress, "Подключение к базе данных...", 0, 4)
            adapter.connect(conn_cfg)

            self._emit(progress, "Получение структуры базы данных...", 1, 4)
            structure = adapter.get_database_structure()

            target = output_dir / conn_cfg.database
            self._emit(progress, f"Генерация SQL-скриптов в: {target}", 2, 4)
            result = self.generator.generate_scripts(structure, target)

            self._emit(progress, "Готово", 4, 4)
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
    """Convenience constructor for CLI/GUI."""
    return ReverseEngineerService()

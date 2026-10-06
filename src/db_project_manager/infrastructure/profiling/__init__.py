"""Table-profiling SQL generators registry (Phase 20).

Рецепт нового движка — docstring ``base.ProfilingSQLGenerator``; выбор
генератора по типу подключения — здесь (по образцу ``registry.get_adapter``).
"""

from __future__ import annotations

from db_project_manager.infrastructure.profiling.base import (
    ColumnCategory,
    ProfilingColumn,
    ProfilingSQLGenerator,
    TableMeta,
    categorize,
    quote_ident,
    quote_literal,
)
from db_project_manager.infrastructure.profiling.greenplum import GreenplumProfiler
from db_project_manager.infrastructure.profiling.postgres import PostgresProfiler

__all__ = [
    "ColumnCategory",
    "GreenplumProfiler",
    "PostgresProfiler",
    "ProfilingColumn",
    "ProfilingSQLGenerator",
    "TableMeta",
    "categorize",
    "get_profiler",
    "quote_ident",
    "quote_literal",
]


def get_profiler(db_type: str) -> ProfilingSQLGenerator:
    """Profiler for a connection db_type; ValueError for unsupported types."""
    normalized = (db_type or "").strip().lower()
    if normalized == "greenplum":
        return GreenplumProfiler()
    if normalized == "postgres":
        return PostgresProfiler()
    raise ValueError(
        f"Профайлинг не поддерживается для типа БД {db_type!r} (postgres | greenplum)"
    )

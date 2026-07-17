"""Registry mapping connection type -> adapter class.

Lets CLI/GUI pick the right adapter from ConnectionConfig.type without knowing
concrete classes. New DBMS support = register an adapter here.
"""

from __future__ import annotations

from db_project_manager.domain.connection import ConnectionConfig
from db_project_manager.infrastructure.database.base import DatabaseAdapter, DatabaseError
from db_project_manager.infrastructure.database.postgres.adapter import PGDatabaseAdapter


def get_adapter(cfg: ConnectionConfig) -> DatabaseAdapter:
    """Return an adapter instance appropriate for the connection type."""
    db_type = cfg.type.lower()
    if db_type in ("postgres", "greenplum"):
        return PGDatabaseAdapter()
    raise DatabaseError(f"Неподдерживаемый тип БД: {cfg.type!r}. Поддерживаются: postgres, greenplum.")

"""Database adapter contract (infrastructure layer).

Phase 1 needs only the reverse-engineering surface: connect + read the full
structure as a nested dict. The full adapter contract (list_objects, get_ddl,
execute_script, create_temp_database) arrives in Phase 2.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from db_project_manager.domain.connection import ConnectionConfig


class DatabaseError(Exception):
    """Raised on connection/query failures inside an adapter."""


class DatabaseAdapter(ABC):
    """Abstract base for database-specific adapters."""

    @abstractmethod
    def connect(self, cfg: ConnectionConfig) -> None:
        """Establish a connection using the given configuration."""

    @abstractmethod
    def disconnect(self) -> None:
        """Close the active connection (no-op if none)."""

    @abstractmethod
    def get_database_structure(self) -> dict[str, Any]:
        """Return the full database structure as a nested dict.

        Shape: {"schemas": [ {"name", "comment", "sequences", "tables",
        "views", "materialized_views", "functions", "procedures", "enums"}, ... ]}
        """

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

    # --- Phase 2: validation-deploy surface ---

    @abstractmethod
    def check_can_create_db(self) -> bool:
        """Whether the current user may CREATE DATABASE.

        Called before attempting validation deploy so a missing privilege is
        reported with a clear message instead of a mid-deploy failure.
        """

    @abstractmethod
    def get_server_timestamp_utc(self) -> str:
        """Return the server's UTC timestamp as YYYYMMDDTHHMMSS.

        Used for unique temp-DB names; reading it from the server avoids
        client/server clock drift in CI.
        """

    @abstractmethod
    def create_database(self, name: str) -> None:
        """Create a fresh database. Name must be validated by the caller."""

    @abstractmethod
    def drop_database(self, name: str) -> None:
        """Drop a database created by create_database. Idempotent on missing."""

    @abstractmethod
    def execute_script(self, script: str) -> None:
        """Execute a single object's SQL script against the current connection.

        Transaction management is the caller's responsibility (DeployValidateService).
        """

"""Database adapter contract (infrastructure layer).

Phase 1 needs only the reverse-engineering surface: connect + read the full
structure as a nested dict. The full adapter contract (list_objects, get_ddl,
execute_script, create_temp_database) arrives in Phase 2.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from db_project_manager.domain.connection import ConnectionConfig
from db_project_manager.domain.deploy import ScriptRecord
from db_project_manager.domain.safety import TablePresenceStats


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
    def create_database(
        self,
        name: str,
        *,
        encoding: str | None = None,
        lc_collate: str | None = None,
        lc_ctype: str | None = None,
        template: str | None = None,
    ) -> None:
        """Create a fresh database. Name must be validated by the caller.

        Optional arguments replicate the source database properties so that a
        validation deploy reproduces the source environment accurately.
        """

    @abstractmethod
    def drop_database(self, name: str) -> None:
        """Drop a database created by create_database. Idempotent on missing."""

    @abstractmethod
    def execute_script(self, script: str) -> None:
        """Execute a single object's SQL script against the current connection.

        Transaction management is the caller's responsibility (DeployValidateService).
        """

    # --- Phase 9: compare feature surface ---

    @abstractmethod
    def get_table_row_counts(self) -> list[dict[str, Any]]:
        """Return estimated row counts (reltuples) for user tables.

        Each dict has keys: ``schema_name``, ``table_name``,
        ``estimated_rows`` (float or None). Used by the compare feature as an
        informational "has data?" marker.
        """

    # --- Phase 11: Safety Gate (deploy analyze) surface ---

    @abstractmethod
    def get_table_presence_stats(self) -> list[TablePresenceStats]:
        """Return normalized presence stats for user tables (SG-5).

        Each adapter maps its own catalog metadata onto the database-agnostic
        :class:`~db_project_manager.domain.safety.TablePresenceStats` model:
        ``estimated_rows`` from planner metadata (never ``COUNT(*)``,
        LESSONS §3) plus an abstract ``confidence`` freshness signal
        (PG/Greenplum: ``pg_class.reltuples`` + ``pg_stat_user_tables``;
        databases without a freshness signal report ``UNKNOWN`` — fail-safe by
        design, the gate treats it as "has data").

        System schemas (``pg_catalog``, ``information_schema`` and analogues)
        are excluded here; the service schema (``__deploy``) is excluded by
        the caller (application layer, SG-M).
        """

    # --- Phase 10: CD Foundation (__deploy schema) surface ---

    @abstractmethod
    def get_schema_version(self, schema_name: str) -> str | None:
        """Return the latest calver version applied to ``schema_name``, or None.

        Reads ``MAX(applied_at)`` (or MAX(id)) row from
        ``<schema_name>.schema_version``. None when the table is empty (first
        deploy) or does not exist yet.

        Used by deploy (version-check, record new version) and by
        reverse-engineer (sync ``manifest.source_version`` from a DB that has
        been deployed to before — Phase 10 S6).
        """

    @abstractmethod
    def record_schema_version(self, schema_name: str, version: str, source: str) -> None:
        """Append a row to ``<schema_name>.schema_version`` (version, source).

        ``schema_name`` is configurable (default ``__deploy``). ``version`` is a
        calver ``YYYY.MM.DD.NN`` string; ``source`` distinguishes the run mode
        (``'validate'`` | ``'deploy'`` | ``'manual'``) for audit. Append-only —
        callers never UPDATE this table.
        """

    @abstractmethod
    def get_script_history(
        self, schema_name: str, script_name: str, script_type: str
    ) -> ScriptRecord | None:
        """Return the state row for ``(script_name, script_type)`` or None.

        ``<schema_name>.script_history`` has PRIMARY KEY ``(script_name,
        script_type)`` — exactly one row per name. The pre/post runner uses
        this as the L1 lookup for the skip/error/execute decision (Phase 10
        vision_final §4.4). Returns None when the script has never run.
        """

    @abstractmethod
    def record_script_execution(
        self,
        schema_name: str,
        record: ScriptRecord,
        deploy_version: str,
        deploy_source: str,
    ) -> None:
        """Atomically record a script execution: state UPSERT + audit INSERT.

        Both writes happen in a single transaction (BEGIN/COMMIT) so state and
        history never diverge:

        * UPSERT into ``<schema_name>.script_history`` keyed by ``(script_name,
          script_type)`` — the "what's currently applied" view (state).
        * INSERT into ``<schema_name>.script_audit_log`` — append-only history
          of every attempt, carrying ``deploy_version``/``deploy_source`` so
          reports (Phase 13 CD-17) can JOIN executions to a specific deploy.
        """

"""Database adapter contract (infrastructure layer).

Phase 1 needs only the reverse-engineering surface: connect + read the full
structure as a nested dict. Subsequent phases extend the contract with their
own sections (validation-deploy, CD Foundation, deploy reset, MCP server).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar

from db_project_manager.domain.connection import ConnectionConfig
from db_project_manager.domain.deploy import ScriptRecord
from db_project_manager.domain.query import ExplainResult, QueryResult
from db_project_manager.domain.safety import TablePresenceStats


class DatabaseError(Exception):
    """Raised on connection/query failures inside an adapter."""


class NotSupportedError(DatabaseError):
    """Optional engine capability that this adapter does not implement.

    Raised by base implementations of optional MCP-surface methods so tooling
    can degrade gracefully ("движок не поддерживает") instead of failing hard.
    """


class DatabaseAdapter(ABC):
    """Abstract base for database-specific adapters.

    Adding a new DBMS (Phase 19 extension recipe):

    1. package ``infrastructure/database/<engine>/`` — ``adapter.py`` (this
       contract) + ``queries.py``;
    2. dispatch entry in ``infrastructure/database/registry.py``;
    3. value in ``domain.connection.SUPPORTED_DB_TYPES``;
    4. driver as an optional pyproject extra (e.g. ``db-project-manager[mssql]``);
    5. sqlglot dialect in ``infrastructure/sql/classify.py:DB_TYPE_DIALECT``.

    No MCP-layer changes are needed: tools speak this contract, not a dialect.
    """

    #: Engine capabilities (Phase 19, MCP surface). Adapters declare what the
    #: engine can enforce server-side; the application layer adapts its
    #: defense-in-depth accordingly (classifier always, server backstop when
    #: available).
    supports_readonly_txn: ClassVar[bool] = False
    supports_statement_timeout: ClassVar[bool] = False

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

    # --- Phase 18: deploy reset (schema wipe) surface ---

    @abstractmethod
    def list_schemas(self) -> list[str]:
        """Return user-visible schema names (system/admin schemas excluded).

        PG/Greenplum: everything except ``pg_*``, ``information_schema`` and —
        on GP connections — the GP admin schemas; the same filter RE applies.
        The service schema (``__deploy``) is NOT excluded here: it is excluded
        by the caller (application layer), which knows its configured name.
        """

    @abstractmethod
    def get_schema_object_counts(self) -> dict[str, int]:
        """Approximate object count per schema (report/confirmation aid).

        Advisory precision only — the reset confirmation prints these numbers
        so the human sees the scale of what is about to be dropped.
        """

    @abstractmethod
    def drop_schema(self, name: str) -> None:
        """``DROP SCHEMA ... CASCADE`` — remove the schema entirely.

        Refuses system/admin schema names (defense in depth on top of the
        service-level guard). Used only for schemas absent from the codebase
        (Phase 18 D9 "junk" schemas).
        """

    @abstractmethod
    def drop_schema_contents(self, schema: str) -> None:
        """Drop every user object inside ``schema``, keep the schema shell.

        The shell survives with its ACLs, owner and default privileges intact
        (Phase 18 D9); a subsequent deploy sees the schema as UNCHANGED and
        rebuilds the contents. Enumerates ALL droppable object kinds of the
        concrete DBMS (PG/GP: incl. external/foreign tables) with per-object
        ``DROP ... CASCADE``; internal dependencies (indexes, constraints,
        triggers) resolve via cascade. Routine drops render the argument
        list even for zero-arg routines — kernels < PG 10 (Greenplum 6)
        require it in the DROP grammar.
        """

    @abstractmethod
    def snapshot_schema_acls(self, schemas: list[str]) -> str:
        """Render an executable SQL snapshot of schema-level privileges.

        Returns dialect SQL (PG/GP: ``ALTER SCHEMA ... OWNER TO`` / ``GRANT``
        / ``ALTER DEFAULT PRIVILEGES``) restoring owner, schema ACLs and
        default privileges of the listed schemas. Insurance artifact written
        BEFORE any reset mutation (Phase 18 D9); rendering belongs to the
        adapter so future DBMS speak their own GRANT dialect.
        """

    @abstractmethod
    def truncate_table(self, schema: str, name: str) -> None:
        """``TRUNCATE TABLE schema.name`` — deploy journal reset (Phase 18 D2).

        Raises :class:`DatabaseError` when the table does not exist (missing
        ``__deploy`` is a legal state the caller reports as a warning).
        """

    @abstractmethod
    def drop_extension(self, name: str) -> None:
        """``DROP EXTENSION ... CASCADE`` (Phase 18 reset pre-step).

        Extension-member objects cannot be dropped individually (PG refuses
        even with CASCADE), so extensions residing in wiped schemas must go
        first; the codebase recreates them via ``CREATE EXTENSION IF NOT
        EXISTS``.
        """

    @abstractmethod
    def list_extensions(self) -> list[dict[str, Any]]:
        """Extensions with the schema their objects live in.

        Shape: ``[{"name", "schema", "version", "comment"}, ...]``. Adapters
        of DBMS without the extension concept return ``[]`` — the reset's
        extension step degrades to a no-op (Phase 18 D11).
        """

    # --- Phase 19: MCP server surface ---

    @abstractmethod
    def run_query(
        self,
        sql: str,
        *,
        max_rows: int = 50,
        timeout_s: int = 60,
        readonly: bool = True,
    ) -> QueryResult:
        """Execute a single statement and return its rows (MCP-2).

        Dialect-agnostic contract:

        * single statement — the caller (application layer) enforces this and
          pre-classifies the SQL; the adapter is the enforcement backstop;
        * ``readonly=True``: the statement must execute inside a read-only
          transaction when ``supports_readonly_txn`` — even a statement that
          escaped classification then fails server-side. Engines without the
          capability execute as usual and note the missing backstop in
          ``QueryResult.notices``;
        * ``timeout_s``: statement-level timeout when
          ``supports_statement_timeout``; otherwise advisory;
        * values must be JSON-serializable (driver types stringified);
        * ``truncated=True`` when the statement produced more than ``max_rows``
          rows (adapters fetch ``max_rows + 1`` to detect it).
        """

    @abstractmethod
    def explain(
        self,
        sql: str,
        *,
        analyze: bool = False,
        fmt: str = "auto",
        timeout_s: int = 60,
    ) -> ExplainResult:
        """Return the execution plan of a single statement (MCP-3).

        ``fmt="auto"`` lets the adapter pick the richest format it can render
        reliably (PG: json with text fallback — Greenplum 6 may not serialize
        its custom plan nodes to JSON). Engine-native formats report their own
        ``ExplainResult.fmt`` (``xml``, ``tabular``, ...). ``analyze=True``
        executes the statement — the application layer gates it to read-only
        statements; adapters wrap the execution in a read-only transaction
        when ``supports_readonly_txn`` (defense in depth).
        """

    def get_top_queries(self, *, sort_by: str = "resources", limit: int = 10) -> list[dict[str, Any]]:
        """Slowest / most resource-heavy statements from engine statistics.

        Optional capability (MCP-3): engines without a statistics view (or
        without the extension loaded) raise :class:`NotSupportedError` from
        this default implementation — tooling reports it as a soft limitation.
        """
        raise NotSupportedError(
            f"Тип БД {type(self).__name__} не реализует get_top_queries (нет статистики запросов)"
        )

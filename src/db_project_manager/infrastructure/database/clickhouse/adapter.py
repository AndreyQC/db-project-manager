"""ClickHouse database adapter.

Implements the contract defined in ``infrastructure.database.base`` against
the ClickHouse wire protocol via the ``clickhouse-connect`` driver.

Phase 2 of task ``20261016_001`` provides:

* ``connect()`` / ``disconnect()`` — driver lifecycle, with **retry on
  ``UNKNOWN_DATABASE``** to absorb the 3-shard cluster desync at
  10.41.254.51:31023 where ``cis_etl`` is present on 2 of 3 replicas.
* ``get_database_structure()`` — RE: one "schema" entry per user-visible
  database, with tables / views / dictionaries populated from
  ``system.tables`` / ``system.columns`` / ``system.dictionaries``.

Not yet implemented (Phase 3+): deploy, MCP surface, safety-gate,
``__deploy`` journal, snapshot/reset.
"""

from __future__ import annotations

from typing import Any, ClassVar

from loguru import logger

from db_project_manager.domain.connection import ConnectionConfig
from db_project_manager.domain.deploy import ScriptRecord
from db_project_manager.domain.query import ExplainResult, QueryResult
from db_project_manager.domain.safety import TablePresenceStats
from db_project_manager.infrastructure.database.base import (
    DatabaseAdapter,
    DatabaseError,
    NotSupportedError,
)
from db_project_manager.infrastructure.database.clickhouse import queries as q

#: Cluster name used for ``ON CLUSTER`` DDL. Verified at 10.41.254.51:31023
#: via ``SELECT cluster FROM system.clusters`` (only one cluster named
#: ``default``). All multi-shard DDL must use this suffix.
ON_CLUSTER_SUFFIX = " ON CLUSTER default"

#: Number of retries for transient cluster-desync errors.
#: ``clickhouse-connect`` (and the underlying HTTP API) may randomly hit a
#: replica where the target database is missing — see checkpoint
#: ``20261017_001_checkpoint.md`` for the cluster layout. The default
#: user has full GRANT OPTION, so retries are safe.
_CLUSTER_RETRY_ATTEMPTS = 5
_CLUSTER_RETRY_PAUSE_S = 1.5


class CHDatabaseAdapter(DatabaseAdapter):
    """Adapter for ClickHouse (single-node or cluster)."""

    # CH has no read-only transactions in the PG sense; statements either
    # mutate or not. We still set ``statement_timeout`` via SETTINGS for
    # long-running RE queries.
    supports_readonly_txn: ClassVar[bool] = False
    supports_statement_timeout: ClassVar[bool] = True

    def __init__(self) -> None:
        self._client: Any = None
        self._cfg: ConnectionConfig | None = None
        self._db: str | None = None  # current database (a.k.a. "schema")

    # ------------------------------------------------------------------ #
    # connection lifecycle
    # ------------------------------------------------------------------ #

    def connect(self, cfg: ConnectionConfig) -> None:
        """Open a ClickHouse HTTP client.

        The driver is ``clickhouse-connect`` (sync). Password is passed via
        the connect kwargs; the database is intentionally **not** set on the
        client (see ``_warn_no_database_in_init``).
        """
        self._cfg = cfg
        self._db = cfg.database
        try:
            import clickhouse_connect  # imported lazily; optional dep
        except ImportError as e:  # pragma: no cover
            raise DatabaseError(
                "ClickHouse driver 'clickhouse-connect' is not installed. "
                "Run `uv sync` or `pip install clickhouse-connect`."
            ) from e

        try:
            client = clickhouse_connect.get_client(
                host=cfg.host,
                port=cfg.port or 8123,
                username=cfg.username,
                password=cfg.password,
                # database is intentionally NOT passed — see class docstring
                secure=bool(cfg.options.get("secure", False)),
                send_receive_timeout=30,
            )
            # Sanity check — issues a trivial query that exercises the wire
            # and the cluster routing.
            client.command("SELECT 1")
            self._client = client
            logger.info(
                f"Подключено к CH: {cfg.host}:{cfg.port}/{cfg.database} "
                f"(user={cfg.username})"
            )
        except Exception as e:
            logger.error(f"Ошибка подключения к CH: {e}")
            raise DatabaseError(f"Ошибка подключения к CH: {e}") from e

    def disconnect(self) -> None:
        """Close the client. ``clickhouse-connect`` is HTTP-based; the
        client object holds a pooled connection that is GC'd on close."""
        if self._client is not None:
            try:
                self._client.close()
            except Exception:  # pragma: no cover
                pass
            self._client = None
            self._cfg = None
            self._db = None

    # ------------------------------------------------------------------ #
    # reverse engineering
    # ------------------------------------------------------------------ #

    def get_database_structure(self) -> dict[str, Any]:
        """Return the full database structure as a nested dict.

        Mirrors ``PGDatabaseAdapter.get_database_structure()`` shape:

        ::

            {
              "schemas": [
                {"name", "comment", "tables", "views",
                 "materialized_views", "dictionaries", "functions",
                 "table_functions"},
                ...
              ],
              "reserved_keywords": [...],   # filled in by classifier caller
              "extensions": [],              # CH has no extension concept
              "database": {"properties", "settings"},
            }

        The current connection's database becomes the *primary* schema
        (first entry); other user-visible DBs follow in name order.
        """
        self._require_connection()
        logger.info("Получение структуры CH")

        primary_db = self._db
        all_dbs = [r[0] for r in self._exec_rows(q.LIST_DATABASES)]
        if not all_dbs:
            logger.warning("Не найдено пользовательских БД в CH")
            return {
                "schemas": [],
                "extensions": [],
                "database": {"properties": {}, "settings": {}},
            }

        # Put the connected DB first so the GUI/CLI default behaviour
        # matches the PG adapter (where the current schema is implicit).
        if primary_db and primary_db in all_dbs:
            all_dbs.remove(primary_db)
            all_dbs.insert(0, primary_db)

        schemas: list[dict[str, Any]] = []
        for db_name in all_dbs:
            schemas.append(self._build_schema(db_name))

        logger.info(
            f"Структура CH получена: databases = {len(schemas)}"
        )
        return {
            "schemas": schemas,
            "extensions": [],  # CH has no extension concept
            "database": {
                "properties": {"name": primary_db or "", "engine": "Atomic"},
                "settings": {},
            },
        }

    # ------------------------------------------------------------------ #
    # Phase 2 validation-deploy stubs (deferred to Phase 3+)
    # ------------------------------------------------------------------ #

    def check_can_create_db(self) -> bool:
        """Whether the current user may ``CREATE DATABASE``."""
        self._require_connection()
        try:
            rows = self._exec_rows(q.SHOW_CURRENT_GRANTS)
        except Exception as e:
            raise DatabaseError(f"SHOW GRANTS failed: {e}") from e
        text = " ".join(" ".join(r).lower() for r in rows)
        return "create database" in text or "all on *.*" in text

    def get_server_timestamp_utc(self) -> str:
        """Server's UTC clock formatted as ``YYYYMMDDTHHMMSS``."""
        self._require_connection()
        rows = self._exec_rows("SELECT toString(toUTC(now()), 'YYYYMMDDTHHMMSS')")
        return str(rows[0][0])

    def create_database(
        self,
        name: str,
        *,
        encoding: str | None = None,
        lc_collate: str | None = None,
        lc_ctype: str | None = None,
        template: str | None = None,
    ) -> None:
        """Create a database (``ON CLUSTER`` for cluster-wide)."""
        self._require_connection()
        self._validate_identifier(name)
        self._cmd_with_retry(f"CREATE DATABASE IF NOT EXISTS {name}{ON_CLUSTER_SUFFIX}")

    def drop_database(self, name: str) -> None:
        """Drop a database if it exists. Idempotent on missing."""
        self._require_connection()
        self._validate_identifier(name)
        self._cmd_with_retry(f"DROP DATABASE IF EXISTS {name}{ON_CLUSTER_SUFFIX}")

    def execute_script(self, script: str) -> None:
        """Execute one DDL/DML statement. Caller owns the logical txn."""
        self._require_connection()
        self._cmd_with_retry(script)

    # ------------------------------------------------------------------ #
    # Phase 9 / 11 — presence stats (basic implementation for now)
    # ------------------------------------------------------------------ #

    def get_table_row_counts(self) -> list[dict[str, Any]]:
        """Estimated row counts per user table (``system.tables.total_rows``)."""
        self._require_connection()
        rows = self._exec_rows(
            """
            SELECT DISTINCT database, name, total_rows
            FROM clusterAllReplicas('default', system.tables)
            WHERE database NOT IN ('system', 'INFORMATION_SCHEMA',
                                   'information_schema', 'default')
              AND engine NOT IN ('View')
              AND name NOT LIKE '.inner%%'
            """
        )
        return [
            {"database": r[0], "table": r[1], "estimated_rows": int(r[2] or 0)}
            for r in rows
        ]

    def get_table_presence_stats(self) -> list[TablePresenceStats]:
        """Normalised presence stats (CH has no PG planner stats, so this
        mirrors ``get_table_row_counts`` 1:1 with no freshness signal).

        CH ``system.tables.total_rows`` is recomputed asynchronously during
        merges; we report it as ``UNKNOWN`` because there's no analyser /
        pg_stat-style staleness indicator, and ``classify_presence`` treats
        ``UNKNOWN`` fail-safe (assumes data is present)."""
        from db_project_manager.domain.safety import StatsConfidence

        return [
            TablePresenceStats(
                object_schema=r["database"],
                name=r["table"],
                estimated_rows=r["estimated_rows"],
                confidence=StatsConfidence.UNKNOWN,
            )
            for r in self.get_table_row_counts()
        ]

    # ------------------------------------------------------------------ #
    # Phase 10 — CD Foundation (__deploy journal)
    #
    # Unlike PG, the CH deploy journal lives in a separate ``__deploy``
    # database (not per-target). Every row carries a ``target_db``
    # discriminator. All writes use ON CLUSTER default so the journal is
    # available on every replica.
    # ------------------------------------------------------------------ #

    def get_schema_version(self, schema_name: str) -> str | None:
        """Latest calver version applied to ``schema_name`` (target DB) or None."""
        self._require_connection()
        self._validate_identifier(schema_name)
        try:
            rows = self._exec_rows(
                q.GET_SCHEMA_VERSION, {"schema": schema_name}
            )
        except Exception as e:
            raise DatabaseError(f"Не удалось прочитать schema_version: {e}") from e
        if not rows:
            return None
        return str(rows[0][0])

    def record_schema_version(
        self, schema_name: str, version: str, source: str
    ) -> None:
        """Append a row to ``__deploy.schema_version`` (append-only)."""
        self._require_connection()
        self._validate_identifier(schema_name)
        self._cmd_with_retry_params(
            q.INSERT_SCHEMA_VERSION,
            {"schema": schema_name, "version": version, "source": source},
        )

    def get_script_history(
        self, schema_name: str, script_name: str, script_type: str
    ) -> ScriptRecord | None:
        """State row for ``(schema_name, script_name, script_type)`` or None."""
        self._require_connection()
        self._validate_identifier(schema_name)
        try:
            rows = self._exec_rows(
                q.GET_SCRIPT_HISTORY,
                {
                    "schema": schema_name,
                    "script_name": script_name,
                    "script_type": script_type,
                },
            )
        except Exception as e:
            raise DatabaseError(
                f"Не удалось прочитать script_history: {e}"
            ) from e
        if not rows:
            return None
        (sn, st, checksum, success, err, dur, executed_at) = rows[0]
        return ScriptRecord(
            script_name=sn,
            script_type=st,
            checksum=checksum,
            success=bool(success),
            executed_at=executed_at,
            error_message=err or None,
            duration_ms=int(dur or 0),
        )

    def record_script_execution(
        self,
        schema_name: str,
        record: ScriptRecord,
        deploy_version: str,
        deploy_source: str,
    ) -> None:
        """Atomic state UPSERT + audit INSERT in one txn.

        CH has no multi-statement txn in the PG sense, but ReplacingMergeTree
        guarantees the latest row wins on read (with ``FINAL``). We issue
        the UPSERT and the audit row in sequence; the latter is idempotent
        (append-only), the former collapses to the latest via FINAL."""
        self._require_connection()
        self._validate_identifier(schema_name)
        common = {
            "schema": schema_name,
            "script_name": record.script_name,
            "script_type": record.script_type,
            "checksum": record.checksum,
            "success": 1 if record.success else 0,
            "error_message": record.error_message or "",
            "duration_ms": int(record.duration_ms or 0),
            "deploy_version": deploy_version or "",
            "deploy_source": deploy_source or "",
        }
        self._cmd_with_retry_params(q.UPSERT_SCRIPT_HISTORY, common)
        self._cmd_with_retry_params(q.INSERT_SCRIPT_AUDIT, common)

    @staticmethod
    def _render_named(template: str, **params: Any) -> str:
        """Expand a query with ``%(name)s``-style named parameters into a
        fully-bound SQL string. Kept for callers that need raw SQL (e.g.
        chained string operations); prefer ``_cmd_with_retry_params`` for
        normal parameter binding — it uses the driver's parameter binding
        and is injection-safe.

        Values are single-quoted; strings are escaped by doubling quotes.
        """
        out = template
        for key, val in params.items():
            if isinstance(val, str):
                esc = val.replace("'", "''")
                out = out.replace(f"%({key})s", f"'{esc}'")
            else:
                out = out.replace(f"%({key})s", str(val))
        return out

    # ------------------------------------------------------------------ #
    # Phase 18 — deploy reset (basic implementations for now)
    # ------------------------------------------------------------------ #

    def list_schemas(self) -> list[str]:
        """List user-visible databases (system / INFORMATION_SCHEMA excluded)."""
        self._require_connection()
        return [r[0] for r in self._exec_rows(q.LIST_DATABASES)]

    def get_schema_object_counts(self) -> dict[str, int]:
        """Approx object count per user database (advisory)."""
        self._require_connection()
        rows = self._exec_rows(
            """
            SELECT database, count() AS cnt
            FROM clusterAllReplicas('default', system.tables)
            WHERE database NOT IN ('system', 'INFORMATION_SCHEMA',
                                   'information_schema', 'default')
              AND name NOT LIKE '.inner%%'
            GROUP BY database
            """
        )
        return {r[0]: int(r[1]) for r in rows}

    def drop_schema(self, name: str) -> None:
        self._require_connection()
        self._validate_identifier(name)
        self._cmd_with_retry(f"DROP DATABASE {name}{ON_CLUSTER_SUFFIX}")

    def drop_schema_contents(self, schema: str) -> None:
        """Drop every user object inside ``schema``, keep the shell."""
        self._require_connection()
        self._validate_identifier(schema)
        # Drop tables, views, dictionaries, MVs — in that order so MVs are
        # gone before their underlying tables.
        for kind_clause in (
            "engine IN ('MaterializedView')",
            "engine IN ('View')",
            "engine NOT IN ('View', 'MaterializedView', 'Dictionary')",
        ):
            rows = self._exec_rows(
                f"SELECT DISTINCT name FROM clusterAllReplicas('default', system.tables) "
                f"WHERE database = %(db)s AND {kind_clause}",
                {"db": schema},
            )
            for (name,) in rows:
                self._cmd_with_retry(
                    f"DROP TABLE IF EXISTS {schema}.{name}{ON_CLUSTER_SUFFIX}"
                )
        # Dictionaries live in system.dictionaries; their DROP syntax is
        # ``DROP DICTIONARY``.
        rows = self._exec_rows(
            "SELECT DISTINCT name FROM clusterAllReplicas('default', system.dictionaries) "
            "WHERE database = %(db)s",
            {"db": schema},
        )
        for (name,) in rows:
            self._cmd_with_retry(
                f"DROP DICTIONARY IF EXISTS {schema}.{name}{ON_CLUSTER_SUFFIX}"
            )

    def snapshot_schema_acls(self, schemas: list[str]) -> str:
        """CH has no per-schema ACLs in the PG sense; emit empty snapshot."""
        for s in schemas:
            self._validate_identifier(s)
        return ""

    def truncate_table(self, schema: str, name: str) -> None:
        """``ALTER TABLE ... DELETE WHERE 1`` (TRUNCATE not supported on
        MergeTree in older CH versions)."""
        self._require_connection()
        self._validate_identifier(schema)
        self._validate_identifier(name)
        self._cmd_with_retry(f"ALTER TABLE {schema}.{name} DELETE WHERE 1")

    def drop_extension(self, name: str) -> None:
        raise NotSupportedError("ClickHouse has no extension concept.")

    def list_extensions(self) -> list[dict[str, Any]]:
        return []

    # ------------------------------------------------------------------ #
    # Phase 19 — MCP surface
    # ------------------------------------------------------------------ #

    def run_query(
        self,
        sql: str,
        *,
        max_rows: int = 50,
        timeout_s: int = 60,
        readonly: bool = True,
    ) -> QueryResult:
        """Execute one statement; CH has no read-only txn so the classifier
        upstream is the only safety net. ``max_rows`` enforced client-side
        by slicing ``result.result_rows``."""
        import time

        self._require_connection()
        start = time.monotonic()
        try:
            result = self._client.query(sql, settings={"max_execution_time": timeout_s})
        except Exception as e:
            raise DatabaseError(f"CH query failed: {e}") from e
        columns = list(result.column_names)
        all_rows = [r for r in result.result_rows]
        truncated = len(all_rows) > max_rows
        rows_dicts = [dict(zip(columns, r)) for r in all_rows[:max_rows]]
        return QueryResult(
            rows=rows_dicts,
            columns=columns,
            row_count=len(all_rows),
            truncated=truncated,
            duration_ms=int((time.monotonic() - start) * 1000),
        )

    def explain(
        self,
        sql: str,
        *,
        analyze: bool = False,
        fmt: str = "auto",
        timeout_s: int = 60,
    ) -> ExplainResult:
        """``EXPLAIN`` is supported in ClickHouse (AST / SYNTAX / PIPELINE
        / PLAN / ESTIMATE / QUERY TREE). We always emit PLAN."""
        self._require_connection()
        try:
            res = self._client.query(
                f"EXPLAIN {sql}",
                settings={"max_execution_time": timeout_s},
            )
        except Exception as e:
            raise DatabaseError(f"EXPLAIN failed: {e}") from e
        plan_text = "\n".join(r[0] for r in res.result_rows)
        return ExplainResult(plan=plan_text, format="text", analyzed=analyze)

    def get_top_queries(
        self,
        *,
        sort_by: str = "resources",
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Slowest / most-resource-heavy statements from ``system.query_log``."""
        self._require_connection()
        # CH query_log column names differ by version; use the modern
        # ``total_time`` / ``query_duration_ms`` (CH 23+).
        try:
            rows = self._exec_rows(
                """
                SELECT
                    query_kind,
                    type,
                    query,
                    toFloat64(query_duration_ms) / 1000.0 AS duration_s,
                    read_rows,
                    memory_usage
                FROM system.query_log
                WHERE type != 'QueryStart'
                  AND query_kind != 'Unknown'
                ORDER BY duration_s DESC
                LIMIT %(limit)s
                """,
                {"limit": limit},
            )
        except Exception as e:
            # Older CH versions: column might be missing. Fall back to a
            # safer shape so the MCP tool still returns *something*.
            logger.warning(f"get_top_queries fallback: {e}")
            rows = []
        return [
            {
                "kind": r[0],
                "type": r[1],
                "query": r[2],
                "duration_s": float(r[3]) if r[3] is not None else 0.0,
                "read_rows": int(r[4] or 0),
                "memory_usage": int(r[5] or 0),
            }
            for r in rows
        ]

    # ------------------------------------------------------------------ #
    # internal helpers
    # ------------------------------------------------------------------ #

    def _require_connection(self) -> None:
        if self._client is None:
            raise DatabaseError("CH adapter is not connected; call connect() first.")

    @staticmethod
    def _validate_identifier(name: str) -> None:
        """Whitelist ``[A-Za-z_][A-Za-z0-9_]*`` — same rule as
        ``PGDatabaseAdapter._validate_db_name`` to keep DDL injection-safe
        (LESSONS §19)."""
        import re

        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
            raise DatabaseError(f"Недопустимое имя идентификатора: {name!r}")

    def _exec_rows(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[tuple[Any, ...]]:
        """Run a SELECT and return list of tuples. Retries on transient
        ``UNKNOWN_DATABASE`` to absorb the 3-shard cluster desync at
        10.41.254.51:31023 (see class docstring)."""
        import time

        last: Exception | None = None
        for attempt in range(_CLUSTER_RETRY_ATTEMPTS):
            try:
                res = self._client.query(sql, parameters=params or {})
                return [tuple(r) for r in res.result_rows]
            except Exception as e:
                last = e
                if "UNKNOWN_DATABASE" not in str(e) or attempt == _CLUSTER_RETRY_ATTEMPTS - 1:
                    raise DatabaseError(
                        f"CH SELECT failed: {e}\nSQL: {sql[:200]}"
                    ) from e
                logger.debug(
                    f"Cluster desync on SELECT attempt {attempt + 1}/"
                    f"{_CLUSTER_RETRY_ATTEMPTS}, retrying"
                )
                time.sleep(_CLUSTER_RETRY_PAUSE_S)
        raise DatabaseError(f"CH SELECT failed after retries: {last}")

    def _cmd_with_retry(self, sql: str) -> None:
        """Execute a DDL/DML statement with retry on transient
        ``UNKNOWN_DATABASE`` (cluster desync — see class docstring)."""
        import time

        last: Exception | None = None
        for attempt in range(_CLUSTER_RETRY_ATTEMPTS):
            try:
                self._client.command(sql)
                return
            except Exception as e:
                last = e
                if "UNKNOWN_DATABASE" not in str(e) or attempt == _CLUSTER_RETRY_ATTEMPTS - 1:
                    raise DatabaseError(f"CH command failed: {e}\nSQL: {sql[:200]}") from e
                logger.debug(
                    f"Cluster desync on attempt {attempt + 1}/"
                    f"{_CLUSTER_RETRY_ATTEMPTS}, retrying in "
                    f"{_CLUSTER_RETRY_PAUSE_S}s"
                )
                time.sleep(_CLUSTER_RETRY_PAUSE_S)
        # unreachable
        raise DatabaseError(f"CH command failed after retries: {last}")

    def _cmd_with_retry_params(
        self, sql: str, params: dict[str, Any]
    ) -> None:
        """Same as ``_cmd_with_retry`` but with clickhouse_connect-style
        ``%(name)s`` named parameters (avoids SQL-injection on values)."""
        import time

        last: Exception | None = None
        for attempt in range(_CLUSTER_RETRY_ATTEMPTS):
            try:
                self._client.command(sql, parameters=params)
                return
            except Exception as e:
                last = e
                if "UNKNOWN_DATABASE" not in str(e) or attempt == _CLUSTER_RETRY_ATTEMPTS - 1:
                    raise DatabaseError(
                        f"CH command failed: {e}\nSQL: {sql[:200]}"
                    ) from e
                logger.debug(
                    f"Cluster desync on param cmd attempt {attempt + 1}/"
                    f"{_CLUSTER_RETRY_ATTEMPTS}, retrying"
                )
                time.sleep(_CLUSTER_RETRY_PAUSE_S)
        raise DatabaseError(f"CH command failed after retries: {last}")

    def _build_schema(self, db: str) -> dict[str, Any]:
        """Build one ``schema_info`` entry for a CH database."""
        # overview
        overview = self._exec_rows(q.GET_DATABASE_OVERVIEW, {"db": db})
        comment = ""
        if overview:
            comment = overview[0][2] or ""

        # tables (non-View, non-MV)
        tables: list[dict[str, Any]] = []
        for row in self._exec_rows(q.LIST_OBJECTS, {"db": db}):
            (
                name,
                engine,
                engine_full,
                total_rows,
                total_bytes,
                mtime,
                create_query,
                partition_key,
                sorting_key,
                primary_key,
                sampling_key,
                storage_policy,
            ) = row
            tables.append(
                {
                    "name": name,
                    "engine": engine,
                    "engine_full": engine_full,
                    "total_rows": int(total_rows or 0),
                    "total_bytes": int(total_bytes or 0),
                    "metadata_modification_time": mtime.isoformat() if mtime else None,
                    "create_table_query": create_query,
                    "partition_key": partition_key,
                    "sorting_key": sorting_key,
                    "primary_key": primary_key,
                    "sampling_key": sampling_key,
                    "storage_policy": storage_policy,
                    "columns": self._build_columns(db, name),
                    "constraints": [],   # CH has no PG-style constraints
                    "skip_indexes": self._build_skip_indexes(db, name),
                    "storage_stats": self._build_storage_stats(db, name),
                }
            )

        # views + materialized views
        views: list[dict[str, Any]] = []
        materialized_views: list[dict[str, Any]] = []
        for row in self._exec_rows(q.LIST_VIEWS, {"db": db}):
            (
                name,
                engine,
                engine_full,
                mtime,
                create_query,
                as_select,
            ) = row
            entry = {
                "name": name,
                "engine": engine,
                "engine_full": engine_full,
                "create_table_query": create_query,
                "definition": as_select or "",
                "columns": self._build_columns(db, name),
            }
            if engine == "MaterializedView":
                materialized_views.append(entry)
            else:
                views.append(entry)

        # dictionaries
        dictionaries: list[dict[str, Any]] = []
        for row in self._exec_rows(q.LIST_DICTIONARIES, {"db": db}):
            (
                name,
                dtype,
                status,
                origin,
                bytes_allocated,
                element_count,
                lifetime_min,
                lifetime_max,
                source,
            ) = row
            dictionaries.append(
                {
                    "name": name,
                    "type": dtype,
                    "status": status,
                    "origin": origin,
                    "source": source,
                    "bytes_allocated": int(bytes_allocated or 0),
                    "element_count": int(element_count or 0),
                    "lifetime_min": int(lifetime_min or 0),
                    "lifetime_max": int(lifetime_max or 0),
                }
            )

        # in-flight mutations (per-database) — for deploy pre-flight check
        pending_mutations: list[dict[str, Any]] = []
        for row in self._exec_rows(q.LIST_PENDING_MUTATIONS, {"db": db}):
            (mtable, mutation_id, command, parts_to_do, is_killed, fail_reason) = row
            pending_mutations.append(
                {
                    "table": mtable,
                    "mutation_id": mutation_id,
                    "command": command,
                    "parts_to_do": int(parts_to_do or 0),
                    "is_killed": bool(is_killed),
                    "fail_reason": fail_reason or None,
                }
            )

        return {
            "name": db,
            "comment": comment,
            "sequences": [],          # CH has no PG sequences
            "tables": tables,
            "views": views,
            "materialized_views": materialized_views,
            "dictionaries": dictionaries,
            "pending_mutations": pending_mutations,
            "functions": [],
            "procedures": [],         # CH has no stored procedures
            "enums": [],              # CH has no enums
            "table_functions": [],
        }

    def _build_skip_indexes(
        self, db: str, table: str
    ) -> list[dict[str, Any]]:
        """One entry per DataSkippingIndex (minmax / set / bloom_filter / ...)."""
        indexes: list[dict[str, Any]] = []
        for row in self._exec_rows(q.LIST_SKIP_INDEXES, {"db": db, "table": table}):
            (
                name,
                idx_type,
                expr,
                granularity,
                compressed,
                uncompressed,
                marks,
            ) = row
            indexes.append(
                {
                    "name": name,
                    "type": idx_type,
                    "expr": expr,
                    "granularity": int(granularity or 0),
                    "data_compressed_bytes": int(compressed or 0),
                    "data_uncompressed_bytes": int(uncompressed or 0),
                    "marks_bytes": int(marks or 0),
                }
            )
        return indexes

    def _build_storage_stats(
        self, db: str, table: str
    ) -> dict[str, int]:
        """Per-table parts / bytes aggregated from ``system.parts``.

        ``active_parts`` = parts that back live queries; ``inactive_parts``
        = merged-out or detached (still on disk until TTL). 0 for views
        (no parts)."""
        rows = self._exec_rows(q.GET_STORAGE_STATS, {"db": db, "table": table})
        if not rows:
            return {
                "active_parts": 0,
                "inactive_parts": 0,
                "parts_total_rows": 0,
                "parts_total_bytes": 0,
            }
        (active, inactive, total_rows, total_bytes) = rows[0]
        return {
            "active_parts": int(active or 0),
            "inactive_parts": int(inactive or 0),
            "parts_total_rows": int(total_rows or 0),
            "parts_total_bytes": int(total_bytes or 0),
        }

    def _build_columns(self, db: str, table: str) -> list[dict[str, Any]]:
        """Build the column list for one object (table/view/MV/dict)."""
        columns: list[dict[str, Any]] = []
        for row in self._exec_rows(q.GET_COLUMNS, {"db": db, "table": table}):
            (
                name,
                ctype,
                default_kind,
                default_expression,
                comment,
                in_pk,
                in_sort,
                in_partition,
                in_sampling,
                codec,
            ) = row
            columns.append(
                {
                    "name": name,
                    "type": ctype,
                    "nullable": True,   # CH columns are nullable unless
                                         # wrapped in a non-nullable engine
                                         # (e.g. NonZero/Nullable) — Phase 3
                                         # to refine from type string.
                    "default": default_expression or None,
                    "default_kind": default_kind or None,
                    "comment": comment or None,
                    "in_primary_key": bool(in_pk),
                    "in_sorting_key": bool(in_sort),
                    "in_partition_key": bool(in_partition),
                    "in_sampling_key": bool(in_sampling),
                    "compression_codec": codec or None,
                }
            )
        return columns

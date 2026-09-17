"""PostgreSQL / Greenplum database adapter.

Reads the database catalog and returns the full structure as a nested dict.
SQL text lives in queries.py. The adapter also serves Greenplum connections
(same wire protocol) with a fallback query for sequences.
"""

from __future__ import annotations

import re
from typing import Any, ClassVar

from loguru import logger
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from db_project_manager.domain.connection import ConnectionConfig, ConnectionType
from db_project_manager.domain.deploy import ScriptRecord
from db_project_manager.domain.safety import StatsConfidence, TablePresenceStats
from db_project_manager.infrastructure.database.base import DatabaseAdapter, DatabaseError
from db_project_manager.infrastructure.database.postgres import queries as q
from db_project_manager.infrastructure.database.postgres.keywords import get_reserved
from db_project_manager.infrastructure.database.ssh_tunnel import SSHTunnelManager

#: Identifier whitelist for temp-DB names (defends against injection in
#: CREATE DATABASE / DROP DATABASE — see LESSONS_LEARNED §create_database).
_DB_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: Serial detection (Phase 16.7, LESSONS §67 family): a column whose default
#: is nextval of the PG-default-named sequence ``<table>_<col>_seq`` renders
#: as serialN — the codebase spelling. Bare ``serial`` is avoided on purpose:
#: sqlglot normalizes it to IDENTITY, which would break the hash compare.
#: The sequence itself is still emitted as a standalone object (pg_dump-style
#: folding is a separate concern).
_SERIAL_NEXTVAL_RE = re.compile(r"^nextval\('([^']+)'::regclass\)$", re.IGNORECASE)
_SERIAL_BY_UDT = {"int2": "smallserial", "int4": "serial4", "int8": "serial8"}

#: Greenplum administrative schemas — product-managed (gp_toolkit views read
#: master/segment logs), never user objects. Excluded from RE only on
#: greenplum connections: they are not extension-owned (pg_depend has no
#: deptype='e' rows for them), so the schema list is the only filter point.
#: ``gp_statistics``/``gp_statistics_history`` exist on GP 7 only — filtering
#: by name is a no-op where they are absent. On PostgreSQL a same-name schema
#: would be a user schema and is kept.
GP_ADMIN_SCHEMAS = frozenset({"gp_toolkit", "gp_statistics", "gp_statistics_history"})


def map_presence_row(
    schema: str,
    name: str,
    estimated_rows: float | int | None,
    last_analyze: Any,
    last_autoanalyze: Any,
    n_mod_since_analyze: int | None,
) -> TablePresenceStats:
    """Map a raw ``GET_TABLE_PRESENCE_STATS`` row onto the normalized model (SG-5).

    Pure function (unit-testable without a DB). Fail-safe by design: anything
    that undermines trust in ``estimated_rows == 0`` reports ``STALE`` — the
    domain then treats the table as having data (SG-4).

    ``STALE`` when:

    * ``estimated_rows`` is NULL or negative (PG's ``-1`` "never analyzed"
      sentinel) — the planner has no usable estimate;
    * the table was never analyzed (``last_analyze`` and ``last_autoanalyze``
      are both NULL);
    * heavy drift — modifications since the last analyze are comparable to the
      whole estimate (``n_mod_since_analyze >= max(estimated_rows, 1)``).

    Note for Greenplum: row estimates of distributed tables may behave
    differently — validate this mapping on a Greenplum cluster separately
    (vision_final §4.4).
    """
    rows: int | None
    if estimated_rows is None:
        rows = None
    else:
        rows = int(estimated_rows)

    confidence = StatsConfidence.FRESH
    if rows is None or rows < 0:
        confidence = StatsConfidence.STALE
    elif last_analyze is None and last_autoanalyze is None:
        confidence = StatsConfidence.STALE
    elif n_mod_since_analyze is not None and n_mod_since_analyze >= max(rows, 1):
        confidence = StatsConfidence.STALE
    return TablePresenceStats(
        object_schema=schema, name=name, estimated_rows=rows, confidence=confidence
    )


def _validate_db_name(name: str) -> str:
    """Reject anything outside [A-Za-z_][A-Za-z0-9_]* before it reaches SQL."""
    if not name or not _DB_NAME_RE.match(name):
        raise DatabaseError(
            f"Недопустимое имя базы данных: {name!r}. "
            "Допускаются только латинские буквы, цифры и подчёркивание."
        )
    return name


# Functions whose string argument is a sequence name; we qualify it with the
# table's schema when it is bare (no schema prefix). Matches 'seq' inside
# nextval('seq'::regclass), currval('seq'), etc.
_SEQ_REF_RE = re.compile(
    r"(?P<prefix>(?:nextval|currval)\s*\(\s*')"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
    r"(?P<suffix>'\s*::\s*regclass\s*\)|'\s*\))"
)


def _qualify_default_schema(default: Any, schema: str) -> Any:
    """Add the table's schema prefix to bare sequence names in a column default.

    ``nextval('audit_log_id_seq'::regclass)`` in a table of schema ``qr`` becomes
    ``nextval('qr.audit_log_id_seq'::regclass)`` so the generated DDL is
    self-contained and deploy order is robust regardless of search_path.

    Already-qualified names (``schema.seq``) are left as-is.
    """
    if not default or not isinstance(default, str) or not schema:
        return default
    return _SEQ_REF_RE.sub(
        lambda m: f"{m['prefix']}{schema}.{m['name']}{m['suffix']}", default
    )


class PGDatabaseAdapter(DatabaseAdapter):
    """Adapter for PostgreSQL (and Greenplum) catalogs."""

    def __init__(self) -> None:
        self._engine: Engine | None = None
        self._connection = None
        self._is_greenplum: bool = False
        self._pg_sequence_available: bool | None = None
        self._prokind_available: bool | None = None
        self._tunnel: SSHTunnelManager | None = None
        self._cfg: ConnectionConfig | None = None

    # --- connection lifecycle ---

    def connect(self, cfg: ConnectionConfig) -> None:
        """Open a connection. Password is passed via args, not the URL.

        If connection_type is SSH_TUNNEL, first establishes an SSH tunnel
        to the jump host and connects through it.
        """
        self._cfg = cfg
        # Capability probes are per-connection (see _get_sequences, _supports_prokind).
        self._pg_sequence_available = None
        self._prokind_available = None
        if cfg.connection_type == ConnectionType.SSH_TUNNEL:
            self._connect_via_ssh_tunnel(cfg)
        else:
            self._connect_direct(cfg)

    def _connect_direct(self, cfg: ConnectionConfig) -> None:
        """Direct connection without SSH tunnel."""
        try:
            url = (
                f"postgresql+psycopg2://{cfg.username}@{cfg.host}:{cfg.port}/{cfg.database}"
            )
            connect_args: dict[str, Any] = {"password": cfg.password}
            connect_args.update(cfg.options)

            self._engine = create_engine(url, isolation_level="AUTOCOMMIT", connect_args=connect_args)
            self._connection = self._engine.connect()
            self._connection.execute(text("SELECT 1"))
            self._is_greenplum = cfg.is_greenplum
            logger.info(f"Подключено к БД: {cfg.host}:{cfg.port}/{cfg.database} (greenplum={self._is_greenplum})")
        except Exception as e:
            logger.error(f"Ошибка подключения к БД: {e}")
            raise DatabaseError(f"Ошибка подключения к БД: {e}") from e

    def _connect_via_ssh_tunnel(self, cfg: ConnectionConfig) -> None:
        """Connect through an SSH tunnel."""
        if cfg.ssh_tunnel is None:
            raise DatabaseError("ssh_tunnel configuration is required for SSH_TUNNEL connection type")

        from db_project_manager.infrastructure.crypto.crypto_util import get_decrypted_text, _is_cipher_token

        logger.info(f"SSH tunnel: starting connection to {cfg.ssh_tunnel.ssh_host}:{cfg.ssh_tunnel.ssh_port}")

        # Decrypt SSH password if needed
        ssh_pass = cfg.ssh_tunnel.ssh_pass
        if _is_cipher_token(ssh_pass):
            logger.info("SSH password is encrypted, decrypting...")
            ssh_pass = get_decrypted_text(ssh_pass)
            logger.info("SSH password decrypted")
        else:
            logger.info("SSH password is plaintext")

        # Create and start tunnel
        logger.info("Creating tunnel manager...")
        tunnel = SSHTunnelManager.from_config(cfg.ssh_tunnel, decrypted_password=ssh_pass)
        logger.info("Starting tunnel (this may take up to 10 seconds)...")
        local_port = tunnel.start()
        logger.info(f"Tunnel started on local port {local_port}")
        self._tunnel = tunnel

        # Connect to database via tunnel
        try:
            url = f"postgresql+psycopg2://{cfg.username}@127.0.0.1:{local_port}/{cfg.database}"
            connect_args: dict[str, Any] = {"password": cfg.password}
            connect_args.update(cfg.options)

            self._engine = create_engine(url, isolation_level="AUTOCOMMIT", connect_args=connect_args)
            self._connection = self._engine.connect()
            self._connection.execute(text("SELECT 1"))
            self._is_greenplum = cfg.is_greenplum
            logger.info(
                f"Подключено к БД через SSH туннель: 127.0.0.1:{local_port}/{cfg.database} "
                f"(tunnel={cfg.ssh_tunnel.ssh_host}:{cfg.ssh_tunnel.ssh_port})"
            )
        except Exception as e:
            self._tunnel.stop()
            self._tunnel = None
            logger.error(f"Ошибка подключения через SSH туннель: {e}")
            raise DatabaseError(f"Ошибка подключения через SSH туннель: {e}") from e

    def disconnect(self) -> None:
        try:
            if self._connection is not None:
                self._connection.close()
        finally:
            if self._engine is not None:
                self._engine.dispose()
            self._connection = None
            self._engine = None
            # Stop SSH tunnel after database connection is closed
            if self._tunnel is not None:
                self._tunnel.stop()
                self._tunnel = None

    def _require_connection(self) -> None:
        if self._connection is None:
            raise DatabaseError("Сначала необходимо подключиться к базе данных")

    # --- Phase 2: validation-deploy surface ---

    def check_can_create_db(self) -> bool:
        self._require_connection()
        try:
            rows = self._exec(q.GET_CREATEDB_CHECK)
            if not rows:
                return False
            return bool(rows[0][0])
        except Exception as e:
            logger.error(f"Не удалось проверить право CREATEDB: {e}")
            raise DatabaseError(f"Не удалось проверить право CREATEDB: {e}") from e

    def get_server_timestamp_utc(self) -> str:
        self._require_connection()
        try:
            rows = self._exec(q.GET_SERVER_TIMESTAMP_UTC)
            if not rows or not rows[0][0]:
                raise DatabaseError("Сервер вернул пустое значение timestamp")
            return str(rows[0][0])
        except DatabaseError:
            raise
        except Exception as e:
            logger.error(f"Не получить timestamp сервера: {e}")
            raise DatabaseError(f"Не получить timestamp сервера: {e}") from e

    def get_table_row_counts(self) -> list[dict[str, Any]]:
        """Return estimated row counts (reltuples) for user tables.

        Phase 9: used by the compare feature as an informational "has data?"
        marker in the snapshot. Returns [] if there are no user tables.
        """
        self._require_connection()
        try:
            rows = self._exec(q.GET_TABLE_ROW_COUNTS)
            infos = [
                {"schema_name": schema, "table_name": name, "estimated_rows": est}
                for schema, name, est in rows
            ]
            logger.info(f"Row counts получены для {len(infos)} таблиц")
            return infos
        except Exception as e:
            logger.error(f"Не удалось получить row counts: {e}")
            raise DatabaseError(f"Не удалось получить row counts: {e}") from e

    def get_table_presence_stats(self) -> list[TablePresenceStats]:
        """Return normalized presence stats for user tables (Phase 11, SG-5).

        Reads planner metadata only (reltuples + pg_stat_user_tables — no
        COUNT(*) scans, LESSONS §3) and maps each row via :func:`map_presence_row`.
        System schemas are excluded by the query; the service schema
        (``__deploy``) is excluded by the caller (application layer, SG-M).
        """
        self._require_connection()
        try:
            rows = self._exec(q.GET_TABLE_PRESENCE_STATS)
            stats = [
                map_presence_row(schema, name, est, la, laa, nmod)
                for schema, name, est, la, laa, nmod in rows
            ]
            logger.info(f"Presence stats получены для {len(stats)} таблиц")
            return stats
        except Exception as e:
            logger.error(f"Не удалось получить presence stats: {e}")
            raise DatabaseError(f"Не удалось получить presence stats: {e}") from e

    def create_database(
        self,
        name: str,
        *,
        encoding: str | None = None,
        lc_collate: str | None = None,
        lc_ctype: str | None = None,
        template: str | None = None,
    ) -> None:
        """Create a fresh database. Autocommit is required (DDL outside tx).

        Optional args replicate source-db CREATE DATABASE properties:
        encoding, lc_collate, lc_ctype, template. Template requires an existing
        DB; when the template does not exist on the server we fall back to
        template0 with a warning (vision §6 risk mitigation).
        """
        self._require_connection()
        _validate_db_name(name)
        parts = [f'CREATE DATABASE "{name}"']
        if encoding:
            # ENCODING accepts a number or a quoted string.
            parts.append(f"ENCODING '{encoding}'")
        if lc_collate:
            # Locale values with dots/spaces MUST be single-quoted.
            parts.append(f"LC_COLLATE '{lc_collate}'")
        if lc_ctype:
            parts.append(f"LC_CTYPE '{lc_ctype}'")
        if template:
            # template must exist; fallback is callers' responsibility.
            parts.append(f"TEMPLATE \"{template}\"")
        sql = " ".join(parts) + ";"
        try:
            self._connection.execute(text(sql))
            logger.info(f"Создана база данных: {name}" + (f" ({', '.join(p for p in parts[1:])} )" if len(parts) > 1 else ""))
        except Exception as e:
            # Fallback: if locale/encoding is not supported on the target server,
            # retry with template0 (standard locale) and without custom locale settings.
            # This is the risk mitigation from Phase 5 vision §6.
            if not template:
                fallback_sql = f'CREATE DATABASE "{name}" TEMPLATE template0;'
                logger.warning(
                    f"Не удалось создать БД с указанными локалью/кодировкой ({e}). "
                    f"Повторная попытка через template0..."
                )
                try:
                    self._connection.execute(text(fallback_sql))
                    logger.info(f"Создана база данных (fallback template0): {name}")
                    return
                except Exception as fe:
                    logger.error(f"Ошибка создания базы данных (fallback тоже не удался): {fe}")
                    raise DatabaseError(f"Ошибка создания базы данных: {fe}") from fe
            raise DatabaseError(f"Ошибка создания базы данных {name}: {e}") from e

    def drop_database(self, name: str) -> None:
        """Drop a database. Idempotent: missing database is not an error."""
        self._require_connection()
        _validate_db_name(name)
        try:
            self._connection.execute(text(f'DROP DATABASE IF EXISTS "{name}";'))
            logger.info(f"Удалена база данных: {name}")
        except Exception as e:
            logger.error(f"Ошибка удаления базы данных {name}: {e}")
            raise DatabaseError(f"Ошибка удаления базы данных {name}: {e}") from e

    def execute_script(self, script: str) -> None:
        self._require_connection()
        try:
            self._connection.execute(text(script))
        except Exception as e:
            raise DatabaseError(f"Ошибка выполнения скрипта: {e}") from e

    # --- structure aggregation ---

    def get_database_structure(self) -> dict[str, Any]:
        """Return the full database structure as a nested dict."""
        self._require_connection()
        logger.info("Получение структуры базы данных")

        schemas: list[dict[str, Any]] = []
        for schema in self._get_schemas():
            schema_info: dict[str, Any] = {
                "name": schema["name"],
                "comment": schema["comment"],
                "sequences": [],
                "tables": [],
                "views": [],
                "materialized_views": [],
                "functions": [],
                "procedures": [],
                "enums": [],
            }
            schema_name = schema["name"]
            gp_table_options = self._get_gp_table_options(schema_name)

            schema_info["tables"] = [
                self._build_table(schema_name, table, gp_table_options.get(table["name"]))
                for table in self._get_tables(schema_name)
            ]
            schema_info["sequences"] = self._get_sequences(schema_name)
            schema_info["views"] = self._get_views(schema_name)
            schema_info["materialized_views"] = self._get_materialized_views(schema_name)
            schema_info["functions"] = self._get_functions(schema_name)
            schema_info["procedures"] = self._get_procedures(schema_name)

            schemas.append(schema_info)

        extensions = self._get_extensions()
        database = {
            "properties": self._get_database_properties(),
            "settings": self._get_database_settings(),
        }

        logger.info(f"Структура получена: схем = {len(schemas)}, extensions = {len(extensions)}")
        return {
            "schemas": schemas,
            "reserved_keywords": get_reserved(),
            # Phase 5: extensions and db-level settings are global, not per-schema.
            "extensions": extensions,
            "database": database,
        }

    # --- Phase 10: CD Foundation (__deploy schema) surface ---

    @staticmethod
    def _quote_identifier(name: str) -> str:
        """Quote a SQL identifier (schema/table/column) for safe interpolation.

        Whitelist ``[A-Za-z_][A-Za-z0-9_]*`` (LESSONS §19 — same rule as
        ``_validate_db_name``). The ``__deploy`` default and any user-chosen
        ``deploy.service_schema`` value pass; arbitrary input is rejected to
        keep the ``{schema}`` interpolation in queries.py injection-safe.
        """
        import re

        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
            raise DatabaseError(f"Недопустимое имя идентификатора: {name!r}")
        return f'"{name}"'

    def get_schema_version(self, schema_name: str) -> str | None:
        """Latest calver version recorded in ``<schema>.schema_version`` or None."""
        self._require_connection()
        schema = self._quote_identifier(schema_name)
        try:
            rows = self._exec(q.GET_SCHEMA_VERSION.format(schema=schema))
        except Exception as e:
            # Table missing (deploy never ran) is a normal first-time case;
            # surface as DatabaseError only for unexpected failures.
            raise DatabaseError(f"Не удалось прочитать schema_version: {e}") from e
        if not rows:
            return None
        return str(rows[0][0])

    def record_schema_version(self, schema_name: str, version: str, source: str) -> None:
        """Append a row to ``<schema>.schema_version`` (version, source)."""
        self._require_connection()
        schema = self._quote_identifier(schema_name)
        try:
            self._connection.execute(
                text(q.INSERT_SCHEMA_VERSION.format(schema=schema)),
                {"version": version, "source": source},
            )
        except Exception as e:
            raise DatabaseError(f"Не удалось записать schema_version: {e}") from e

    def get_script_history(
        self, schema_name: str, script_name: str, script_type: str
    ) -> ScriptRecord | None:
        """Return the state row for ``(script_name, script_type)`` or None."""
        self._require_connection()
        schema = self._quote_identifier(schema_name)
        try:
            rows = self._exec(
                q.GET_SCRIPT_HISTORY.format(schema=schema),
                {"script_name": script_name, "script_type": script_type},
            )
        except Exception as e:
            raise DatabaseError(f"Не удалось прочитать script_history: {e}") from e
        if not rows:
            return None
        r = rows[0]
        return ScriptRecord(
            script_name=str(r[0]),
            script_type=str(r[1]),  # type: ignore[arg-type]
            checksum=str(r[2]),
            success=bool(r[3]),
            error_message=None if r[4] is None else str(r[4]),
            duration_ms=int(r[5]),
            executed_at=r[6],
        )

    def record_script_execution(
        self,
        schema_name: str,
        record: ScriptRecord,
        deploy_version: str,
        deploy_source: str,
    ) -> None:
        """Atomically UPSERT state + INSERT audit log (single transaction).

        The shared ``self._connection`` runs in AUTOCOMMIT — to get a real
        transaction we open a fresh connection off the engine and downgrade
        its isolation level for the duration of these two writes. If the audit
        INSERT fails after the state UPSERT, both roll back (state and history
        never diverge).
        """
        self._require_connection()
        assert self._engine is not None
        schema = self._quote_identifier(schema_name)
        params_state = {
            "script_name": record.script_name,
            "script_type": record.script_type,
            "checksum": record.checksum,
            "success": record.success,
            "executed_at": record.executed_at,
            "error_message": record.error_message,
            "duration_ms": record.duration_ms,
        }
        params_audit = {
            **params_state,
            "deploy_version": deploy_version,
            "deploy_source": deploy_source,
        }
        try:
            with self._engine.connect().execution_options(
                isolation_level="READ_COMMITTED"
            ) as tx_conn:
                with tx_conn.begin():
                    tx_conn.execute(
                        text(q.UPSERT_SCRIPT_HISTORY.format(schema=schema)),
                        params_state,
                    )
                    tx_conn.execute(
                        text(q.INSERT_SCRIPT_AUDIT_LOG.format(schema=schema)),
                        params_audit,
                    )
        except Exception as e:
            raise DatabaseError(f"Не удалось записать script execution: {e}") from e

    # --- Phase 18: deploy reset (schema wipe) surface ---

    #: relkind -> DROP verb for content-drop (Phase 18 D9). 'r'/'p' tables and
    #: partitioned tables, GP external tables ('x'), foreign tables ('f').
    _RELKIND_DROP: ClassVar[dict[str, str]] = {
        "r": "TABLE",
        "p": "TABLE",
        "v": "VIEW",
        "m": "MATERIALIZED VIEW",
        "S": "SEQUENCE",
        "f": "FOREIGN TABLE",
        "x": "EXTERNAL TABLE",
    }

    def _assert_reset_allowed_schema(self, schema: str) -> None:
        """Refuse system/GP-admin schemas in any reset DDL (defense in depth).

        The service layer guards the configured service schema; this adapter
        guard covers everything the platform itself owns.
        """
        if schema.startswith("pg_") or schema == "information_schema":
            raise DatabaseError(f"Отказ: системная схема не может быть сброшена: {schema!r}")
        if schema in GP_ADMIN_SCHEMAS:
            raise DatabaseError(f"Отказ: админ-схема GP не может быть сброшена: {schema!r}")

    def list_schemas(self) -> list[str]:
        """User-visible schema names (RE filter: pg_*/information_schema/GP admin)."""
        self._require_connection()
        return [s["name"] for s in self._get_schemas()]

    def get_schema_object_counts(self) -> dict[str, int]:
        self._require_connection()
        try:
            rows = self._exec(q.GET_SCHEMA_OBJECT_COUNTS)
        except Exception as e:
            raise DatabaseError(f"Не удалось получить счётчики объектов схем: {e}") from e
        return {str(name): int(count or 0) for name, count in rows}

    def drop_schema(self, name: str) -> None:
        self._require_connection()
        self._assert_reset_allowed_schema(name)
        quoted = self._quote_identifier(name)
        try:
            self._connection.execute(text(f"DROP SCHEMA IF EXISTS {quoted} CASCADE;"))
            logger.info(f"Схема удалена (DROP SCHEMA CASCADE): {name}")
        except Exception as e:
            raise DatabaseError(f"Не удалось удалить схему {name!r}: {e}") from e

    def drop_schema_contents(self, schema: str) -> None:
        """Content-drop (Phase 18 D9): objects go, the schema shell and its
        ACLs/owner/default privileges stay. Every object drops with CASCADE in
        its own AUTOCOMMIT statement; the first failure stops the wipe
        (stop-on-error) so the reset report shows exactly where it broke.
        """
        self._require_connection()
        self._assert_reset_allowed_schema(schema)
        quoted_schema = self._quote_identifier(schema)
        try:
            objects = self._exec(q.GET_SCHEMA_DROPPABLE_OBJECTS, {"schema": schema})
        except Exception as e:
            raise DatabaseError(f"Не удалось прочитать объекты схемы {schema!r}: {e}") from e
        routines_query = (
            q.GET_SCHEMA_ROUTINES_POSTGRES if self._supports_prokind()
            else q.GET_SCHEMA_ROUTINES_GREENPLUM
        )
        try:
            routines = self._exec(routines_query, {"schema": schema})
        except Exception as e:
            raise DatabaseError(f"Не удалось прочитать функции схемы {schema!r}: {e}") from e

        dropped = 0
        try:
            for name, relkind in objects:
                verb = self._RELKIND_DROP.get(relkind)
                if verb is None:  # defensive: query filters to known relkinds
                    logger.warning(f"Пропущен объект {schema}.{name} с relkind={relkind!r}")
                    continue
                ident = f"{quoted_schema}.{self._quote_identifier(name)}"
                self._connection.execute(text(f"DROP {verb} IF EXISTS {ident} CASCADE;"))
                dropped += 1
            for name, args, prokind in routines:
                verb = {"p": "PROCEDURE", "a": "AGGREGATE"}.get(prokind, "FUNCTION")
                ident = f"{quoted_schema}.{self._quote_identifier(name)}"
                if args:
                    ident = f"{ident}({args})"
                self._connection.execute(text(f"DROP {verb} IF EXISTS {ident} CASCADE;"))
                dropped += 1
        except Exception as e:
            raise DatabaseError(
                f"Сброс содержимого схемы {schema!r} прерван на объекте №{dropped + 1}: {e}"
            ) from e
        logger.info(f"Content-drop схемы '{schema}': удалено объектов {dropped}.")

    def snapshot_schema_acls(self, schemas: list[str]) -> str:
        """Render owner/GRANT/ALTER DEFAULT PRIVILEGES for ``schemas`` (D9).

        Insurance artifact, not executed by the tool: written next to the
        reset report so a human can restore privileges by hand if a wipe went
        further than intended. Role names are not tool-controlled, so they are
        escaped (doubled quotes) rather than whitelist-validated.
        """
        self._require_connection()
        header = (
            "-- db-pm deploy reset: ACL snapshot (schema owner / grants /\n"
            "-- default privileges). Insurance artifact — restore manually if\n"
            "-- needed. Generated for schemas: " + ", ".join(schemas) + "\n"
        )
        if not schemas:
            return header + "-- (нет схем)\n"
        try:
            acl_rows = self._exec(q.GET_SCHEMA_ACL_SNAPSHOT, {"schemas": list(schemas)})
            defacl_rows = self._exec(q.GET_DEFAULT_ACL_SNAPSHOT, {"schemas": list(schemas)})
        except Exception as e:
            raise DatabaseError(f"Не удалось прочитать ACL схем: {e}") from e

        def _quote_role(role: str | None) -> str:
            return '"' + str(role).replace('"', '""') + '"'

        lines: list[str] = [header]
        seen: set[str] = set()
        for schema_name, owner, privilege, grantee_oid, grantee_name in acl_rows:
            if schema_name not in seen:
                seen.add(schema_name)
                lines.append(f'\n-- Schema "{schema_name}"')
                lines.append(f"ALTER SCHEMA {self._quote_identifier(schema_name)} "
                             f"OWNER TO {_quote_role(owner)};")
            if privilege is None:  # nspacl NULL/empty — owner-only entry
                continue
            grantee = "PUBLIC" if not grantee_oid else _quote_role(grantee_name)
            lines.append(
                f"GRANT {privilege} ON SCHEMA "
                f"{self._quote_identifier(schema_name)} TO {grantee};"
            )
        objtype_map = {"r": "TABLES", "S": "SEQUENCES", "f": "FUNCTIONS", "T": "TYPES"}
        for schema_name, role_name, objtype, privilege, grantee_oid, grantee_name in defacl_rows:
            target = objtype_map.get(objtype)
            if target is None:
                lines.append(f"-- неизвестный defaclobjtype={objtype!r} (schema "
                             f"{schema_name}, role {role_name}) — пропущен")
                continue
            grantee = "PUBLIC" if not grantee_oid else _quote_role(grantee_name)
            lines.append(
                f"ALTER DEFAULT PRIVILEGES FOR ROLE {_quote_role(role_name)} "
                f"IN SCHEMA {self._quote_identifier(schema_name)} "
                f"GRANT {privilege or 'USAGE'} ON {target} TO {grantee};"
            )
        if not acl_rows and not defacl_rows:
            lines.append("-- (права не найдены)\n")
        return "\n".join(lines) + "\n"

    def truncate_table(self, schema: str, name: str) -> None:
        self._require_connection()
        ident = f"{self._quote_identifier(schema)}.{self._quote_identifier(name)}"
        try:
            self._connection.execute(text(f"TRUNCATE TABLE {ident};"))
            logger.info(f"Таблица очищена: {schema}.{name}")
        except Exception as e:
            raise DatabaseError(f"Не удалось очистить таблицу {schema}.{name}: {e}") from e

    def drop_extension(self, name: str) -> None:
        self._require_connection()
        quoted = self._quote_identifier(name)
        try:
            self._connection.execute(text(f"DROP EXTENSION IF EXISTS {quoted} CASCADE;"))
            logger.info(f"Extension удалён (CASCADE): {name}")
        except Exception as e:
            raise DatabaseError(f"Не удалось удалить extension {name!r}: {e}") from e

    def list_extensions(self) -> list[dict[str, Any]]:
        self._require_connection()
        return self._get_extensions()

    # --- helpers: low-level readers (kept close to the POC result shape) ---

    def _exec(self, query: str, params: dict[str, Any] | None = None) -> list:
        assert self._connection is not None
        result = self._connection.execute(text(query), params)
        return result.fetchall()

    def _get_schemas(self) -> list[dict[str, Any]]:
        rows = self._exec(q.GET_SCHEMAS)
        if self._is_greenplum:
            dropped = sorted(row[0] for row in rows if row[0] in GP_ADMIN_SCHEMAS)
            if dropped:
                logger.info(f"Админ-схемы GP исключены из RE: {', '.join(dropped)}")
            rows = [row for row in rows if row[0] not in GP_ADMIN_SCHEMAS]
        infos = [{"name": row[0], "comment": row[1]} for row in rows]
        logger.info(f"Схем найдено: {len(infos)}")
        return infos

    def _get_tables(self, schema: str) -> list[dict[str, Any]]:
        rows = self._exec(q.GET_TABLES, {"schema": schema})
        infos = [{"name": name, "comment": comment} for name, comment in rows]
        logger.info(f"Таблиц в '{schema}': {len(infos)}")
        return infos

    def _get_gp_table_options(self, schema: str) -> dict[str, dict[str, Any]]:
        """Greenplum-only table properties: distribution + storage options.

        Returns {} on PostgreSQL (gp_distribution_policy does not exist there;
        a same-name catalog would be a user object — LESSONS §71-3). One query
        per schema, not per table.
        """
        if not self._is_greenplum:
            return {}
        rows = self._exec(q.GET_TABLE_GP_OPTIONS, {"schema": schema})
        options: dict[str, dict[str, Any]] = {}
        for name, policytype, distkey_columns, reloptions in rows:
            distribution: dict[str, Any] | None = None
            if policytype == "r":
                distribution = {"kind": "replicated", "columns": []}
            elif distkey_columns:
                distribution = {
                    "kind": "by",
                    "columns": [c.strip() for c in distkey_columns.split(",") if c.strip()],
                }
            else:
                # policytype 'p' with an empty distkey — the cluster-wide
                # default on cis_zup_gp_dev (196/196 tables).
                distribution = {"kind": "randomly", "columns": []}
            options[name] = {
                "distribution": distribution,
                "storage_options": list(reloptions) if reloptions else None,
            }
        return options

    def _build_table(
        self,
        schema: str,
        table: dict[str, Any],
        gp_options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        name = table["name"]
        columns = []
        for col in self._exec(q.GET_COLUMNS, {"table_name": name, "schema": schema}):
            col_type, default = col[1], col[3]
            serial = _SERIAL_BY_UDT.get(col_type)
            if serial and default:
                match = _SERIAL_NEXTVAL_RE.match(default.strip())
                if match and match.group(1).lower() == f"{schema}.{name}_{col[0]}_seq".lower():
                    col_type, default = serial, None
            columns.append(
                {
                    "name": col[0],
                    "type": col_type,
                    "nullable": col[2] == "YES",
                    "default": _qualify_default_schema(default, schema),
                    "character_maximum_length": col[4],
                    "numeric_precision": col[5],
                    "numeric_scale": col[6],
                    "comment": col[7],
                }
            )
        constraints = self._group_constraints(
            self._exec(q.GET_CONSTRAINTS, {"table_name": name, "schema": schema})
        )
        # NOT NULL is a column modifier, not a table-level constraint.  In
        # information_schema it appears as constraint_type='CHECK', but
        # pg_get_constraintdef() returns "NOT NULL <colname>" — neither valid as
        # a named CONSTRAINT nor needed because nullable is already derived from
        # col[2] == 'YES'.  Drop these artefacts.
        constraints = [
            c for c in constraints
            if not (c["type"] == "CHECK" and c["definition"].startswith("NOT NULL"))
        ]
        indexes = self._group_indexes(
            self._exec(q.GET_INDEXES, {"table_name": name, "schema": schema})
        )
        return {
            "schema": schema,
            "name": name,
            "comment": table["comment"],
            "columns": columns,
            "constraints": constraints,
            "primary_keys": [],
            "indexes": indexes,
            "distribution": (gp_options or {}).get("distribution"),
            "storage_options": (gp_options or {}).get("storage_options"),
        }

    @staticmethod
    def _group_constraints(rows: list) -> list[dict[str, Any]]:
        grouped: dict[str, dict[str, Any]] = {}
        for (
            cname,
            ctype,
            cschema,
            ctable,
            column_name,
            ref_schema,
            ref_table,
            ref_column,
            definition,
            comment,
        ) in rows:
            entry = grouped.setdefault(
                cname,
                {
                    "name": cname,
                    "type": ctype,
                    "schema": cschema,
                    "table_name": ctable,
                    "columns": [],
                    "referenced_table_schema": ref_schema,
                    "referenced_table_name": ref_table,
                    "referenced_column_name": ref_column,
                    "definition": definition,
                    "comment": comment,
                },
            )
            if column_name is not None:
                entry["columns"].append(column_name)
        return list(grouped.values())

    @staticmethod
    def _group_indexes(rows: list) -> list[dict[str, Any]]:
        grouped: dict[str, dict[str, Any]] = {}
        for index_name, column_name, index_type, unique, filter_condition in rows:
            entry = grouped.setdefault(
                index_name,
                {
                    "name": index_name,
                    "columns": [],
                    "index_type": index_type,
                    "unique": unique,
                    "filter_condition": filter_condition,
                },
            )
            if column_name is not None:
                entry["columns"].append(column_name)
        return list(grouped.values())

    def _get_sequences(self, schema: str) -> list[dict[str, Any]]:
        # pg_sequence exists in PG 10+ and Greenplum 7 (kernel PG 12) but not in
        # Greenplum 6 (kernel PG 9.4) — hence a capability probe cached for the
        # connection, not a branch on _is_greenplum: GP 7 must keep the richer
        # pg_sequence query, GP 6 must not retry the doomed query per schema.
        if self._pg_sequence_available is None:
            try:
                rows = self._exec(q.GET_SEQUENCES_POSTGRES, {"schema": schema})
                self._pg_sequence_available = True
            except Exception as e:
                if not self._is_greenplum:
                    raise
                self._pg_sequence_available = False
                reason = str(e).splitlines()[0]
                logger.info(
                    f"pg_sequence недоступен ({reason}) — ожидаемо для ядра GP < PG 10; "
                    "далее используется Greenplum-фолбэк без повторных проб."
                )
                rows = self._exec(q.GET_SEQUENCES_GREENPLUM, {"schema": schema})
        elif self._pg_sequence_available:
            rows = self._exec(q.GET_SEQUENCES_POSTGRES, {"schema": schema})
        else:
            rows = self._exec(q.GET_SEQUENCES_GREENPLUM, {"schema": schema})
        infos = [
            {
                "name": r[0],
                "schema": r[1],
                "owning_table": r[2],
                "owning_column": r[3],
                "data_type": r[4],
                "start": r[5],
                "increment": r[6],
                "maxvalue": r[7],
                "minvalue": r[8],
                "cache": r[9],
                "cycle": r[10],
                "last_value": r[11],
                "comment": r[12],
            }
            for r in rows
        ]
        logger.info(f"Последовательностей в '{schema}': {len(infos)}")
        return infos

    def _get_views(self, schema: str) -> list[dict[str, Any]]:
        rows = self._exec(q.GET_VIEWS, {"schema": schema})
        grouped: dict[str, dict[str, Any]] = {}
        for view_name, vschema, comment, definition, column_name, column_comment in rows:
            entry = grouped.setdefault(
                view_name,
                {"name": view_name, "schema": vschema, "comment": comment, "definition": definition, "columns": []},
            )
            entry["columns"].append({"name": column_name, "comment": column_comment})
        logger.info(f"Представлений в '{schema}': {len(grouped)}")
        return list(grouped.values())

    def _get_materialized_views(self, schema: str) -> list[dict[str, Any]]:
        rows = self._exec(q.GET_MATERIALIZED_VIEWS, {"schema": schema})
        grouped: dict[str, dict[str, Any]] = {}
        for view_name, vschema, tablespace, data_status, comment, definition, column_name, column_comment in rows:
            entry = grouped.setdefault(
                view_name,
                {
                    "name": view_name,
                    "schema": vschema,
                    "tablespace": tablespace,
                    "data_status": data_status,
                    "comment": comment,
                    "definition": (definition or "").strip().rstrip(";"),
                    "columns": [],
                },
            )
            entry["columns"].append({"name": column_name, "comment": column_comment})
        logger.info(f"Мат. представлений в '{schema}': {len(grouped)}")
        return list(grouped.values())

    def _supports_prokind(self) -> bool:
        """Whether pg_proc.prokind is available (PG 11+ / Greenplum 7).

        Probed once per connection and cached (same pattern as the pg_sequence
        probe): Greenplum 6 (kernel PG 9.4) lacks the column and uses the
        legacy proisagg/proiswindow queries instead.
        """
        if self._prokind_available is None:
            try:
                self._exec(q.PROKIND_PROBE)
                self._prokind_available = True
            except Exception as e:
                if not self._is_greenplum:
                    raise
                self._prokind_available = False
                reason = str(e).splitlines()[0]
                logger.info(
                    f"pg_proc.prokind недоступен ({reason}) — ожидаемо для ядра GP < PG 11; "
                    "функции читаются legacy-запросом, процедуры ядром не поддерживаются."
                )
        return self._prokind_available

    def _get_functions(self, schema: str) -> list[dict[str, Any]]:
        query = q.GET_FUNCTIONS_POSTGRES if self._supports_prokind() else q.GET_FUNCTIONS_GREENPLUM
        rows = self._exec(query, {"schema": schema})
        infos = [
            {
                "name": r[0],
                "schema": r[1],
                "return_type": r[2],
                "arguments": r[3],
                "argument_types": r[4],
                "language": r[5],
                "returns_set": r[6],
                "definition": r[7],
                "comment": r[8],
            }
            for r in rows
        ]
        logger.info(f"Функций в '{schema}': {len(infos)}")
        return infos

    # --- Phase 5: extensions and database settings (global, not per-schema) ---

    def _get_extensions(self) -> list[dict[str, Any]]:
        rows = self._exec(q.GET_EXTENSIONS)
        infos = [
            {"name": r[0], "schema": r[1], "version": r[2], "comment": r[3]}
            for r in rows
        ]
        logger.info(f"Extensions найдено: {len(infos)}")
        return infos

    def _get_database_properties(self) -> dict[str, Any]:
        """Behaviour-relevant properties of the current database (vision Q8)."""
        rows = self._exec(q.GET_DATABASE_PROPERTIES)
        if not rows:
            logger.warning("pg_database не вернул свойств для текущей БД")
            return {}
        encoding, lc_collate, lc_ctype = rows[0]
        return {"encoding": encoding, "lc_collate": lc_collate, "lc_ctype": lc_ctype}

    def _get_database_settings(self) -> list[dict[str, Any]]:
        """Explicitly set db-level parameters (setrole = 0 — vision Q5).

        Each pg_db_role_setting.setconfig element is a "param=value" string.
        """
        rows = self._exec(q.GET_DATABASE_SETTINGS)
        settings: list[dict[str, Any]] = []
        for (setting,) in rows:
            name, sep, value = str(setting).partition("=")
            if not sep or not name:
                logger.warning(f"Пропущен нераспознанный параметр БД: {setting!r}")
                continue
            settings.append({"name": name, "value": value})
        logger.info(f"Параметров уровня БД: {len(settings)}")
        return settings

    def _get_procedures(self, schema: str) -> list[dict[str, Any]]:
        if not self._supports_prokind():
            # Kernels without prokind (PG <= 10, Greenplum 6) have no CREATE
            # PROCEDURE at all — prokind='p' has nothing to match.
            logger.info(f"Процедур в '{schema}': 0 (ядро без CREATE PROCEDURE)")
            return []
        rows = self._exec(q.GET_PROCEDURES_POSTGRES, {"schema": schema})
        infos = [
            {
                "name": r[0],
                "schema": r[1],
                "arguments": r[2],
                "argument_types": r[3],
                "language": r[4],
                "returns_set": r[5],
                "definition": r[6],
                "comment": r[7],
            }
            for r in rows
        ]
        logger.info(f"Процедур в '{schema}': {len(infos)}")
        return infos

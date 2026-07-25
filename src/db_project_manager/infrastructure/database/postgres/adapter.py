"""PostgreSQL / Greenplum database adapter.

Reads the database catalog and returns the full structure as a nested dict.
SQL text lives in queries.py. The adapter also serves Greenplum connections
(same wire protocol) with a fallback query for sequences.
"""

from __future__ import annotations

import re
from typing import Any

from loguru import logger
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from db_project_manager.domain.connection import ConnectionConfig, ConnectionType
from db_project_manager.infrastructure.database.base import DatabaseAdapter, DatabaseError
from db_project_manager.infrastructure.database.postgres import queries as q
from db_project_manager.infrastructure.database.postgres.keywords import get_reserved
from db_project_manager.infrastructure.database.ssh_tunnel import SSHTunnelManager

#: Identifier whitelist for temp-DB names (defends against injection in
#: CREATE DATABASE / DROP DATABASE — see LESSONS_LEARNED §create_database).
_DB_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


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
        self._tunnel: SSHTunnelManager | None = None
        self._cfg: ConnectionConfig | None = None

    # --- connection lifecycle ---

    def connect(self, cfg: ConnectionConfig) -> None:
        """Open a connection. Password is passed via args, not the URL.

        If connection_type is SSH_TUNNEL, first establishes an SSH tunnel
        to the jump host and connects through it.
        """
        self._cfg = cfg
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

            schema_info["tables"] = [
                self._build_table(schema_name, table) for table in self._get_tables(schema_name)
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

    # --- helpers: low-level readers (kept close to the POC result shape) ---

    def _exec(self, query: str, params: dict[str, Any] | None = None) -> list:
        assert self._connection is not None
        result = self._connection.execute(text(query), params)
        return result.fetchall()

    def _get_schemas(self) -> list[dict[str, Any]]:
        rows = self._exec(q.GET_SCHEMAS)
        infos = [{"name": row[0], "comment": row[1]} for row in rows]
        logger.info(f"Схем найдено: {len(infos)}")
        return infos

    def _get_tables(self, schema: str) -> list[dict[str, Any]]:
        rows = self._exec(q.GET_TABLES, {"schema": schema})
        infos = [{"name": name, "comment": comment} for name, comment in rows]
        logger.info(f"Таблиц в '{schema}': {len(infos)}")
        return infos

    def _build_table(self, schema: str, table: dict[str, Any]) -> dict[str, Any]:
        name = table["name"]
        columns = [
            {
                "name": col[0],
                "type": col[1],
                "nullable": col[2] == "YES",
                "default": _qualify_default_schema(col[3], schema),
                "character_maximum_length": col[4],
                "numeric_precision": col[5],
                "numeric_scale": col[6],
                "comment": col[7],
            }
            for col in self._exec(q.GET_COLUMNS, {"table_name": name, "schema": schema})
        ]
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
        try:
            rows = self._exec(q.GET_SEQUENCES_POSTGRES, {"schema": schema})
        except Exception as e:
            if not self._is_greenplum:
                raise
            logger.warning(f"pg_sequence недоступен ({e}), использую Greenplum-фолбэк")
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

    def _get_functions(self, schema: str) -> list[dict[str, Any]]:
        rows = self._exec(q.GET_FUNCTIONS, {"schema": schema})
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
        rows = self._exec(q.GET_PROCEDURES, {"schema": schema})
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

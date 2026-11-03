"""Snowflake database adapter (Phase 21, task 20261103_002).

MVP scope (plan 20261103_002): connect with browser SSO registration
(``authenticator="externalbrowser"``, token cached in the OS credential
store) + password fallback, the read surface (``run_query``) and the
reverse-engineering surface (``get_database_structure`` via SHOW +
INFORMATION_SCHEMA + GET_DDL). The deploy/reset/safety surface is a
deliberate stub (:class:`NotSupportedError`) — deploying into Snowflake
is a separate follow-up phase, mirroring the CH adapter's staged rollout.

Object sources (see queries.py docstring):

* listing + comments + query text — ``SHOW`` commands (dict rows);
* columns/constraints — INFORMATION_SCHEMA with bind parameters;
* routine bodies — ``GET_DDL`` (views take their query from SHOW text).

Account identifier mapping (no domain-model changes, plan §Architecture):

* ``options.account`` wins over everything;
* ``host = <account>.privatelink.snowflakecomputing.com`` or
  ``<account>.snowflakecomputing.com`` → account = host without the
  suffix, the host is passed to the driver as-is;
* any other host is treated as a bare (region-qualified) account
  identifier — ``xy12345``, ``xy12345.eu-central-1`` — and is NOT passed
  as ``host`` (the connector builds the URL from the account).

MVP limitations (documented in the plan / result):

* autoincrement columns: Snowflake renders them as ``AUTOINCREMENT`` (no
  PG ``GENERATED ... AS IDENTITY`` analog) — the default
  (``<seq>.NEXTVAL``) is dropped, the column is emitted plain;
* CHECK constraints are skipped (Snowflake CHECK is hybrid-table-only);
* indexes: Snowflake has none on standard tables (hybrid-table indexes
  are out of MVP) — ``indexes: []``.
"""

from __future__ import annotations

import re
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
from db_project_manager.infrastructure.database.postgres.keywords import get_reserved
from db_project_manager.infrastructure.database.snowflake import queries as q

#: Host suffixes Snowflake routes by; the account is the host minus suffix.
_HOST_SUFFIXES = (".privatelink.snowflakecomputing.com", ".snowflakecomputing.com")

#: MVP auth methods (plan 20261103_002, USER_INPUT-1: externalbrowser +
#: password fallback; key-pair is BACKLOG P3).
_AUTH_EXTERNALBROWSER = "externalbrowser"
_AUTH_PASSWORD = "snowflake"
_MVP_AUTHENTICATORS = (_AUTH_EXTERNALBROWSER, _AUTH_PASSWORD)

#: Message shared by all deliberate deploy/reset/safety stubs.
_DEPLOY_STUB_MSG = (
    "Snowflake: деплой-сурфейс вне MVP Phase 21 (20261103_002) — "
    "деплой в Snowflake будет отдельной фазой"
)


def extract_account(cfg: ConnectionConfig) -> str:
    """Return the Snowflake account identifier for the connection config.

    Resolution order (see module docstring): ``options.account`` → host
    minus the Snowflake/privatelink suffix → the host as a bare account
    identifier.
    """
    account = str(cfg.options.get("account") or "").strip().rstrip(".")
    if account:
        return account
    host = (cfg.host or "").strip().rstrip(".")
    lowered = host.lower()
    for suffix in _HOST_SUFFIXES:
        if lowered.endswith(suffix):
            return host[: -len(suffix)]
    return host


def build_connect_kwargs(cfg: ConnectionConfig) -> dict[str, Any]:
    """Build the kwargs dict for ``snowflake.connector.connect``.

    Pure function (no I/O) so unit tests cover the auth/config mapping
    without the driver installed. Raises :class:`DatabaseError` on an
    unsupported authenticator or a missing password for the password flow.
    """
    authenticator = str(cfg.options.get("authenticator") or _AUTH_PASSWORD).lower()
    if authenticator not in _MVP_AUTHENTICATORS:
        raise DatabaseError(
            f"Snowflake MVP поддерживает authenticator 'externalbrowser' (браузерная SSO) "
            f"и 'snowflake' (пароль); получено {authenticator!r}. Key-pair — BACKLOG P3."
        )
    kwargs: dict[str, Any] = {
        "account": extract_account(cfg),
        "user": cfg.username,
        "database": cfg.database,
        "authenticator": authenticator,
    }
    host = (cfg.host or "").strip().rstrip(".")
    # Pass host only when it is a real hostname; a bare account identifier
    # must NOT go into host= (the connector builds the URL from account=).
    if host.lower().endswith(_HOST_SUFFIXES):
        kwargs["host"] = host
    if cfg.port and cfg.port != 443:
        kwargs["port"] = cfg.port
    for opt_key, kwarg in (("warehouse", "warehouse"), ("role", "role"), ("schema", "schema")):
        if cfg.options.get(opt_key):
            kwargs[kwarg] = cfg.options[opt_key]
    if authenticator == _AUTH_EXTERNALBROWSER:
        # Cache the SSO token in the OS credential store (Windows: WAM /
        # Credential Manager) — the browser opens once per token lifetime.
        # Requires the connector's secure-local-storage extra (in [snowflake]).
        if cfg.options.get("client_store_temporary_credential", True):
            kwargs["client_store_temporary_credential"] = True
        # The password field is ignored by the browser flow — not passed.
    else:
        if not cfg.password:
            raise DatabaseError(
                "authenticator 'snowflake' требует password "
                "(crypto__… в connections/*.yaml). Для входа через браузер "
                "укажите authenticator: externalbrowser."
            )
        kwargs["password"] = cfg.password
    return kwargs


def _json_safe(value: Any) -> Any:
    """Stringify driver-specific types so rows stay JSON-serializable."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


class SnowflakeAdapter(DatabaseAdapter):
    """Adapter for Snowflake (Phase 21 MVP: connect + run_query; RE — S2)."""

    # Snowflake has no read-only transactions in the PG sense, and the
    # per-statement timeout (STATEMENT_TIMEOUT_IN_SECONDS) is a session-level
    # ALTER — out of MVP. The upstream SQL classifier is the only safety net.
    supports_readonly_txn: ClassVar[bool] = False
    supports_statement_timeout: ClassVar[bool] = False

    def __init__(self) -> None:
        self._conn: Any = None
        self._cfg: ConnectionConfig | None = None

    # ------------------------------------------------------------------ #
    # connection lifecycle
    # ------------------------------------------------------------------ #

    def connect(self, cfg: ConnectionConfig) -> None:
        """Open a Snowflake connection.

        The driver is ``snowflake-connector-python`` (optional extra
        ``[snowflake]``), imported lazily so the dev environment does not
        need it. Browser SSO opens the default browser on first connect;
        subsequent connects reuse the cached token when
        ``client_store_temporary_credential`` is enabled (default).
        """
        self._cfg = cfg
        try:
            from snowflake import connector as sf_connector  # lazy; optional dep
        except ImportError as e:  # pragma: no cover - depends on env
            raise DatabaseError(
                "Snowflake driver 'snowflake-connector-python' is not installed. "
                "Run: uv sync --extra snowflake"
            ) from e

        kwargs = build_connect_kwargs(cfg)
        try:
            conn = sf_connector.connect(**kwargs)
        except Exception as e:
            raise DatabaseError(
                f"Не удалось подключиться к Snowflake ({cfg.host}, "
                f"authenticator={kwargs.get('authenticator')!r}): {e}"
            ) from e
        try:
            cur = conn.cursor()
            cur.execute(q.PING)
            cur.close()
        except Exception as e:
            try:
                conn.close()
            except Exception:  # noqa: S110 - best-effort cleanup
                pass
            raise DatabaseError(f"Snowflake: sanity-запрос не выполнен: {e}") from e
        self._conn = conn
        logger.info(
            f"Подключено к Snowflake: account={kwargs.get('account')!r} "
            f"db={cfg.database!r} (user={cfg.username}, "
            f"auth={kwargs.get('authenticator')!r})"
        )

    def disconnect(self) -> None:
        """Close the active connection (no-op if none)."""
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                logger.warning("Snowflake: ошибка при закрытии соединения (проигнорирована)")
            finally:
                self._conn = None

    def _require_connection(self) -> None:
        if self._conn is None:
            raise DatabaseError("Нет активного подключения к Snowflake (вызовите connect)")

    # ------------------------------------------------------------------ #
    # Phase 1: reverse-engineering surface
    # ------------------------------------------------------------------ #

    #: Identifier whitelist for SHOW-scope/GET_DDL interpolation (SHOW cannot
    #: bind parameters; GET_DDL's fq string goes through a bind anyway).
    _IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")

    #: System schema excluded from RE (the only one INFORMATION_SCHEMA-level).
    _SYSTEM_SCHEMAS = frozenset({"INFORMATION_SCHEMA"})

    def get_database_structure(self) -> dict[str, Any]:
        """Return the full database structure as a nested dict.

        Same shape as the PG adapter ({"schemas": [...], "reserved_keywords",
        "extensions", "database"}) so SQLGenerator renders unchanged. Per
        schema: tables (regular + transient), views, materialized_views,
        sequences, functions, procedures; ``indexes: []`` (Snowflake has no
        standard-table indexes), ``enums: []``.
        """
        self._require_connection()
        db = self._cfg.database if self._cfg else None
        self._validate_ident(db, "database")
        db_scope = f"DATABASE {self._qident(db)}"

        schemas = []
        for row in self._show("SCHEMAS", db_scope):
            name = str(row.get("name") or "")
            if not name or name.upper() in self._SYSTEM_SCHEMAS:
                continue
            schemas.append(self._get_schema_structure(db, name, row.get("comment")))

        logger.info(f"Структура получена: схем = {len(schemas)}")
        return {
            "schemas": schemas,
            "reserved_keywords": get_reserved(),
            "extensions": [],
            "database": {"properties": {}, "settings": []},
        }

    def _get_schema_structure(self, db: str, schema: str, comment: Any) -> dict[str, Any]:
        return {
            "name": schema,
            "comment": comment,
            "sequences": self._get_sequences(db, schema),
            "tables": self._get_tables(db, schema),
            "indexes": [],
            "views": self._get_views(db, schema),
            "materialized_views": self._get_matviews(db, schema),
            "functions": self._get_routines(db, schema, "USER FUNCTIONS", "FUNCTION"),
            "procedures": self._get_routines(db, schema, "PROCEDURES", "PROCEDURE"),
            "enums": [],
        }

    # --- SHOW / GET_DDL plumbing -------------------------------------------

    @classmethod
    def _validate_ident(cls, name: Any, what: str = "identifier") -> str:
        if not cls._IDENT_RE.match(str(name or "")):
            raise DatabaseError(f"Недопустимое имя {what}: {name!r}")
        return str(name)

    @classmethod
    def _qident(cls, name: str) -> str:
        """Quote an already-validated identifier for SHOW scope text."""
        cls._validate_ident(name)
        return f'"{name}"'

    def _show(self, command: str, scope: str) -> list[dict[str, Any]]:
        """Run a SHOW command, return rows as dicts keyed by column name."""
        cur = self._conn.cursor()
        try:
            cur.execute(f"SHOW {command} {scope}")
            cols = [d[0] for d in cur.description or []]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
        except Exception as e:
            raise DatabaseError(f"Snowflake SHOW {command} failed: {e}") from e
        finally:
            cur.close()

    def _exec(self, query: str, params: dict[str, Any] | None = None) -> list[tuple]:
        """Run a bind-parameter query, return raw rows."""
        cur = self._conn.cursor()
        try:
            cur.execute(query, params or {})
            return list(cur.fetchall())
        except Exception as e:
            raise DatabaseError(f"Snowflake query failed: {e}") from e
        finally:
            cur.close()

    def _get_ddl(self, kind: str, fq: str) -> str:
        """Object DDL via GET_DDL; kind is whitelist-validated (queries.py)."""
        if kind not in q.GET_DDL_KINDS:
            raise DatabaseError(f"Недопустимый GET_DDL kind: {kind!r}")
        rows = self._exec(q.GET_DDL_SQL, {"kind": kind, "fq": fq})
        return str(rows[0][0] or "").strip().rstrip(";") if rows else ""

    # --- per-kind builders ---------------------------------------------------

    def _get_tables(self, db: str, schema: str) -> list[dict[str, Any]]:
        tables = []
        for row in self._show("TABLES", f"SCHEMA {self._qident(db)}.{self._qident(schema)}"):
            name = str(row.get("name") or "")
            if not name:
                continue
            tables.append(self._build_table(db, schema, name, row.get("comment")))
        return tables

    def _build_table(self, db: str, schema: str, name: str, comment: Any) -> dict[str, Any]:
        col_rows = self._exec(q.GET_COLUMNS, {"schema": schema, "table": name})
        columns = []
        for col_name, col_type, nullable, col_default, char_len, num_p, num_s, col_comment in col_rows:
            default = col_default
            is_identity = False
            # Snowflake autoincrement surfaces as <schema>.<seq>.NEXTVAL in
            # COLUMN_DEFAULT; PG's GENERATED AS IDENTITY template branch does
            # not apply (Snowflake uses AUTOINCREMENT) — MVP drops the clause
            # (plan: MVP limitations).
            if default and "nextval" in str(default).lower():
                default, is_identity = None, False
            columns.append(
                {
                    "name": col_name,
                    "type": col_type,
                    "nullable": nullable == "YES",
                    "default": default,
                    "character_maximum_length": char_len,
                    "numeric_precision": num_p,
                    "numeric_scale": num_s,
                    "comment": col_comment,
                    "is_identity": is_identity,
                    "identity_generation": None,
                }
            )
        return {
            "schema": schema,
            "name": name,
            "comment": comment,
            "columns": columns,
            "constraints": self._get_constraints(schema, name),
            "primary_keys": [],
            "indexes": [],
            "distribution": None,
            "storage_options": None,
        }

    def _get_constraints(self, schema: str, table: str) -> list[dict[str, Any]]:
        """Group INFORMATION_SCHEMA constraint rows into PG-shaped dicts.

        ``definition`` is rendered here (PK/UNIQUE column lists, FK targets)
        because the shared table template interpolates it verbatim.
        """
        rows = self._exec(q.GET_CONSTRAINTS, {"schema": schema, "table": table})
        grouped: dict[str, dict[str, Any]] = {}
        for (
            cname,
            ctype,
            ctable,
            col_name,
            ref_cname,
            ref_schema,
            ref_table,
            ref_col,
        ) in rows:
            if ctype == "CHECK":
                continue  # hybrid-table-only in Snowflake; not part of MVP
            entry = grouped.setdefault(
                str(cname),
                {
                    "name": cname,
                    "type": ctype,
                    "schema": schema,
                    "table_name": ctable,
                    "columns": [],
                    "referenced_table_schema": ref_schema,
                    "referenced_table_name": ref_table,
                    "referenced_column_name": ref_col,
                    "definition": "",
                    "comment": None,
                },
            )
            if col_name is not None:
                entry["columns"].append(str(col_name))
        for entry in grouped.values():
            cols = ", ".join(entry["columns"])
            if entry["type"] == "FOREIGN KEY":
                entry["definition"] = (
                    f"FOREIGN KEY ({cols}) REFERENCES "
                    f"{entry['referenced_table_schema']}.{entry['referenced_table_name']}"
                    f" ({entry['referenced_column_name']})"
                )
            elif entry["type"] == "PRIMARY KEY":
                entry["definition"] = f"PRIMARY KEY ({cols})"
            elif entry["type"] == "UNIQUE":
                entry["definition"] = f"UNIQUE ({cols})"
        return list(grouped.values())

    def _get_views(self, db: str, schema: str) -> list[dict[str, Any]]:
        views = []
        for row in self._show("VIEWS", f"SCHEMA {self._qident(db)}.{self._qident(schema)}"):
            name = str(row.get("name") or "")
            if not name:
                continue
            # The shared templates render "AS{{ definition }}" — pg_get_viewdef
            # arrives with leading whitespace, SHOW text does not: add one.
            definition = " " + str(row.get("text") or "").strip().rstrip(";")
            columns = [
                {"name": c[0], "comment": c[7]}
                for c in self._exec(q.GET_COLUMNS, {"schema": schema, "table": name})
            ]
            views.append(
                {
                    "schema": schema,
                    "name": name,
                    "comment": row.get("comment"),
                    "definition": definition,
                    "columns": columns,
                }
            )
        return views

    def _get_matviews(self, db: str, schema: str) -> list[dict[str, Any]]:
        matviews = []
        for row in self._show("MATERIALIZED VIEWS", f"SCHEMA {self._qident(db)}.{self._qident(schema)}"):
            name = str(row.get("name") or "")
            if not name:
                continue
            definition = " " + str(row.get("text") or "").strip().rstrip(";")
            columns = [
                {"name": c[0], "comment": c[7]}
                for c in self._exec(q.GET_COLUMNS, {"schema": schema, "table": name})
            ]
            matviews.append(
                {
                    "schema": schema,
                    "name": name,
                    # No tablespaces in Snowflake — the shared template skips
                    # the clause (Phase 21); PG always passes one (pg_default).
                    "tablespace": None,
                    "data_status": "",
                    "comment": row.get("comment"),
                    "definition": definition,
                    "columns": columns,
                }
            )
        return matviews

    def _get_sequences(self, db: str, schema: str) -> list[dict[str, Any]]:
        """Sequences from SHOW + GET_DDL field parsing.

        Snowflake sequences only carry START/INCREMENT (no MINVALUE/MAXVALUE/
        CACHE/CYCLE) — the shared template skips absent clauses.
        """
        ddl_re_start = re.compile(r"start\s*=\s*(\d+)", re.IGNORECASE)
        ddl_re_incr = re.compile(r"increment\s*=\s*(\d+)", re.IGNORECASE)
        sequences = []
        for row in self._show("SEQUENCES", f"SCHEMA {self._qident(db)}.{self._qident(schema)}"):
            name = str(row.get("name") or "")
            if not name:
                continue
            ddl = self._get_ddl("SEQUENCE", f"{db}.{schema}.{name}")
            m_start = ddl_re_start.search(ddl)
            m_incr = ddl_re_incr.search(ddl)
            sequences.append(
                {
                    "schema": schema,
                    "name": name,
                    "comment": row.get("comment"),
                    "owning_table": None,
                    "owning_column": None,
                    "data_type": "NUMBER",
                    "start": int(m_start.group(1)) if m_start else 1,
                    "increment": int(m_incr.group(1)) if m_incr else 1,
                    "maxvalue": None,
                    "minvalue": None,
                    "cache": None,
                    "cycle": None,
                    "last_value": None,
                }
            )
        return sequences

    def _get_routines(self, db: str, schema: str, show_command: str, ddl_kind: str) -> list[dict[str, Any]]:
        """User functions / procedures via SHOW + GET_DDL body.

        SHOW's ``arguments`` column carries the signature —
        ``NAME(TYPE, ...) RETURN TYPE`` for functions, ``NAME(TYPE, ...)``
        for procedures; the parenthesized part becomes ``argument_types``
        (overload key) and the GET_DDL fq string.
        """
        routines = []
        for row in self._show(show_command, f"SCHEMA {self._qident(db)}.{self._qident(schema)}"):
            name = str(row.get("name") or "")
            args_sig = str(row.get("arguments") or "")
            if not name:
                continue
            m = re.match(rf"^{re.escape(name)}\s*\((.*)\)", args_sig, re.IGNORECASE | re.DOTALL)
            arg_types = m.group(1).strip() if m else ""
            fq = f"{db}.{schema}.{name}({arg_types})" if arg_types else f"{db}.{schema}.{name}"
            definition = self._get_ddl(ddl_kind, fq)
            routines.append(
                {
                    "schema": schema,
                    "name": name,
                    "argument_types": arg_types,
                    "definition": definition,
                    "comment": row.get("description") or row.get("comment"),
                }
            )
        return routines

    # ------------------------------------------------------------------ #
    # Phase 19: MCP query surface
    # ------------------------------------------------------------------ #

    def run_query(
        self,
        sql: str,
        *,
        max_rows: int = 50,
        timeout_s: int = 60,
        readonly: bool = True,
    ) -> QueryResult:
        """Execute one statement; no read-only backstop (see ClassVars)."""
        import time

        self._require_connection()
        start = time.monotonic()
        cur = self._conn.cursor()
        try:
            cur.execute(sql)
            columns = [d[0] for d in cur.description] if cur.description else []
            rows_raw = cur.fetchmany(max_rows + 1) if columns else []
        except Exception as e:
            raise DatabaseError(f"Snowflake query failed: {e}") from e
        truncated = len(rows_raw) > max_rows
        rows = [dict(zip(columns, (_json_safe(v) for v in row))) for row in rows_raw[:max_rows]]
        notices = (
            ["Snowflake: read-only backstop отсутствует (нет RO-транзакций)"]
            if readonly
            else []
        )
        return QueryResult(
            columns=columns,
            rows=rows,
            row_count=len(rows),
            truncated=truncated,
            duration_ms=int((time.monotonic() - start) * 1000),
            notices=notices,
        )

    def explain(
        self,
        sql: str,
        *,
        analyze: bool = False,
        fmt: str = "auto",
        timeout_s: int = 60,
    ) -> ExplainResult:
        raise NotSupportedError(
            "Snowflake: EXPLAIN-сурфейс вне MVP Phase 21 (после live-проверок)"
        )

    # ------------------------------------------------------------------ #
    # Deploy / reset / safety surfaces — deliberate stubs (Phase 21 план)
    # ------------------------------------------------------------------ #

    def check_can_create_db(self) -> bool:
        raise NotSupportedError(_DEPLOY_STUB_MSG)

    def get_server_timestamp_utc(self) -> str:
        raise NotSupportedError(_DEPLOY_STUB_MSG)

    def create_database(
        self,
        name: str,
        *,
        encoding: str | None = None,
        lc_collate: str | None = None,
        lc_ctype: str | None = None,
        template: str | None = None,
    ) -> None:
        raise NotSupportedError(_DEPLOY_STUB_MSG)

    def drop_database(self, name: str) -> None:
        raise NotSupportedError(_DEPLOY_STUB_MSG)

    def execute_script(self, script: str) -> None:
        raise NotSupportedError(_DEPLOY_STUB_MSG)

    def get_table_row_counts(self) -> list[dict[str, Any]]:
        raise NotSupportedError(_DEPLOY_STUB_MSG)

    def get_table_presence_stats(self) -> list[TablePresenceStats]:
        raise NotSupportedError(_DEPLOY_STUB_MSG)

    def get_schema_version(self, schema_name: str) -> str | None:
        raise NotSupportedError(_DEPLOY_STUB_MSG)

    def record_schema_version(self, schema_name: str, version: str, source: str) -> None:
        raise NotSupportedError(_DEPLOY_STUB_MSG)

    def get_script_history(
        self, schema_name: str, script_name: str, script_type: str
    ) -> ScriptRecord | None:
        raise NotSupportedError(_DEPLOY_STUB_MSG)

    def record_script_execution(
        self,
        schema_name: str,
        record: ScriptRecord,
        deploy_version: str,
        deploy_source: str,
    ) -> None:
        raise NotSupportedError(_DEPLOY_STUB_MSG)

    def list_schemas(self) -> list[str]:
        raise NotSupportedError(_DEPLOY_STUB_MSG)

    def get_schema_object_counts(self) -> dict[str, int]:
        raise NotSupportedError(_DEPLOY_STUB_MSG)

    def drop_schema(self, name: str) -> None:
        raise NotSupportedError(_DEPLOY_STUB_MSG)

    def drop_schema_contents(self, schema: str) -> None:
        raise NotSupportedError(_DEPLOY_STUB_MSG)

    def snapshot_schema_acls(self, schemas: list[str]) -> str:
        raise NotSupportedError(_DEPLOY_STUB_MSG)

    def truncate_table(self, schema: str, name: str) -> None:
        raise NotSupportedError(_DEPLOY_STUB_MSG)

    def drop_extension(self, name: str) -> None:
        raise NotSupportedError(_DEPLOY_STUB_MSG)

    def list_extensions(self) -> list[dict[str, Any]]:
        raise NotSupportedError(_DEPLOY_STUB_MSG)

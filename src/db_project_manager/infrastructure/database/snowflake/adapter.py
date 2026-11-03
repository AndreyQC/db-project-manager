"""Snowflake database adapter (Phase 21, task 20261103_002).

MVP scope (plan 20261103_002): connect with browser SSO registration
(``authenticator="externalbrowser"``, token cached in the OS credential
store) + password fallback, plus the read surface (``run_query``).
Reverse-engineering (``get_database_structure``) lands in stage S2 of the
same phase; the deploy/reset/safety surface is a deliberate stub
(:class:`NotSupportedError`) — deploying into Snowflake is a separate
follow-up phase, mirroring the CH adapter's staged rollout.

Account identifier mapping (no domain-model changes, plan §Architecture):

* ``options.account`` wins over everything;
* ``host = <account>.privatelink.snowflakecomputing.com`` or
  ``<account>.snowflakecomputing.com`` → account = host without the
  suffix, the host is passed to the driver as-is;
* any other host is treated as a bare (region-qualified) account
  identifier — ``xy12345``, ``xy12345.eu-central-1`` — and is NOT passed
  as ``host`` (the connector builds the URL from the account).
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

    def get_database_structure(self) -> dict[str, Any]:
        raise NotSupportedError(
            "Snowflake: get_database_structure реализуется этапом S2 фазы "
            "20261103_002 (INFORMATION_SCHEMA + GET_DDL)"
        )

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

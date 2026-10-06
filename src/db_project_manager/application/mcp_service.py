"""Application services for the MCP server (Phase 19).

Two pieces:

* :class:`ConnectionManager` — named connections from the connection store,
  lazily connected adapters cached for the server lifetime (SSH tunnels are
  expensive), one lock per adapter (FastMCP runs sync tools in a thread
  executor, SQLAlchemy connections are not thread-safe).
* :class:`MCPQueryService` — policy gates (``mcp:`` block of the connection
  file) around query/explain/script execution. The classifier is the first
  defense line; the adapter's read-only transaction is the backstop.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from loguru import logger

from db_project_manager.domain.connection import ConnectionConfig
from db_project_manager.domain.query import ExplainResult, QueryResult
from db_project_manager.infrastructure.config.connection_store import ConnectionStore
from db_project_manager.infrastructure.database.base import DatabaseAdapter, DatabaseError
from db_project_manager.infrastructure.database.registry import get_adapter
from db_project_manager.infrastructure.sql.classify import classify_script, dialect_for

AdapterFactory = Callable[[ConnectionConfig], DatabaseAdapter]


class MCPPermissionError(Exception):
    """Policy gate rejection — the call was refused before touching the DB."""


class ManagedConnection:
    """A connected adapter plus the lock serializing its use."""

    def __init__(self, cfg: ConnectionConfig, adapter: DatabaseAdapter) -> None:
        self.cfg = cfg
        self.adapter = adapter
        self.lock = threading.Lock()


class ConnectionManager:
    """Lazily connects named connections and caches the adapters."""

    def __init__(
        self,
        connections_dir: str | Path = "connections",
        adapter_factory: AdapterFactory | None = None,
    ) -> None:
        self._store = ConnectionStore(connections_dir)
        self._adapter_factory = adapter_factory or get_adapter
        self._managed: dict[str, ManagedConnection] = {}
        self._registry_lock = threading.Lock()

    # --- listing (no credentials ever leave this class) ---

    def list_connections(self) -> list[dict[str, Any]]:
        """Connection summaries with effective MCP permissions (MCP-5/7)."""
        result: list[dict[str, Any]] = []
        for name in self._store.list_names():
            try:
                cfg = self.load_config(name)
            except Exception as e:
                result.append({"name": name, "error": f"не удалось прочитать: {e}"})
                continue
            result.append(
                {
                    "name": name,
                    "type": cfg.type,
                    "host": cfg.host,
                    "port": cfg.port,
                    "database": cfg.database,
                    "username": cfg.username,
                    "connection_type": cfg.connection_type.value,
                    "mcp": {
                        "allow_writes": cfg.mcp_settings.allow_writes,
                        "allow_deploy": cfg.mcp_settings.allow_deploy,
                        "row_limit": cfg.mcp_settings.row_limit,
                        "query_timeout_s": cfg.mcp_settings.query_timeout_s,
                    },
                }
            )
        return result

    def load_config(self, name: str) -> ConnectionConfig:
        """Load and decrypt a connection by name (raises when unknown)."""
        return self._store.load_by_name(name)

    # --- connection lifecycle ---

    @contextmanager
    def connection(self, name: str) -> Iterator[ManagedConnection]:
        """Yield a connected adapter; concurrent calls serialize per adapter."""
        managed = self._get_or_connect(name)
        with managed.lock:
            yield managed

    def _get_or_connect(self, name: str) -> ManagedConnection:
        with self._registry_lock:
            managed = self._managed.get(name)
            if managed is not None:
                return managed
            cfg = self.load_config(name)
            adapter = self._adapter_factory(cfg)
            adapter.connect(cfg)
            managed = ManagedConnection(cfg=cfg, adapter=adapter)
            self._managed[name] = managed
            logger.info(f"MCP: подключение установлено — {name} ({cfg.type} {cfg.database})")
            return managed

    def disconnect_all(self) -> None:
        """Close every cached adapter (server shutdown)."""
        with self._registry_lock:
            for name, managed in self._managed.items():
                try:
                    managed.adapter.disconnect()
                except Exception as e:  # pragma: no cover - best-effort cleanup
                    logger.warning(f"MCP: ошибка закрытия подключения {name}: {e}")
            self._managed.clear()


#: Object-type aliases accepted by list_objects/get_object_details, mapped to
#: the structure dict keys produced by DatabaseAdapter.get_database_structure().
OBJECT_TYPE_KEYS: dict[str, str] = {
    "table": "tables",
    "view": "views",
    "materialized_view": "materialized_views",
    "sequence": "sequences",
    "function": "functions",
    "procedure": "procedures",
}


class MCPQueryService:
    """Policy-gated query/explain/script execution (MCP-2..4)."""

    def __init__(self, adapter_factory: AdapterFactory | None = None) -> None:
        self._adapter_factory = adapter_factory or get_adapter

    # --- policy helpers ---

    @staticmethod
    def _require_single_read_only(cfg: ConnectionConfig, sql: str, *, what: str) -> None:
        verdict = classify_script(sql, dialect=dialect_for(cfg.type))
        if len(verdict.statements) != 1:
            raise MCPPermissionError(
                f"{what} принимает ровно один стейтмент (получено {len(verdict.statements)}); "
                "скрипты из нескольких операторов — только через run_script"
            )
        if not verdict.is_read_only:
            raise MCPPermissionError(
                f"{what} выполняет только read-only SQL. Отказано: {verdict.summary()}. "
                "Для изменения данных используйте run_script (требуется mcp.allow_writes)."
            )

    @staticmethod
    def _capped(value: int | None, default: int, cap: int) -> int:
        base = value if value is not None else default
        return max(1, min(base, cap))

    # --- tools ---

    def query(
        self,
        conn: ManagedConnection,
        sql: str,
        *,
        max_rows: int | None = None,
        timeout_s: int | None = None,
    ) -> QueryResult:
        """Read-only single-statement query (MCP-2)."""
        settings = conn.cfg.mcp_settings
        self._require_single_read_only(conn.cfg, sql, what="query")
        effective_rows = self._capped(max_rows, settings.row_limit, settings.row_limit)
        effective_timeout = self._capped(timeout_s, settings.query_timeout_s, settings.query_timeout_s)
        return conn.adapter.run_query(
            sql, max_rows=effective_rows, timeout_s=effective_timeout, readonly=True
        )

    def explain(
        self,
        conn: ManagedConnection,
        sql: str,
        *,
        analyze: bool = False,
        fmt: str = "auto",
        timeout_s: int | None = None,
    ) -> ExplainResult:
        """Execution plan; ANALYZE additionally requires a read-only statement (MCP-3)."""
        settings = conn.cfg.mcp_settings
        if analyze:
            self._require_single_read_only(conn.cfg, sql, what="explain analyze")
        else:
            verdict = classify_script(sql, dialect=dialect_for(conn.cfg.type))
            if len(verdict.statements) != 1:
                raise MCPPermissionError("explain принимает ровно один стейтмент")
        effective_timeout = self._capped(timeout_s, settings.query_timeout_s, settings.query_timeout_s)
        return conn.adapter.explain(sql, analyze=analyze, fmt=fmt, timeout_s=effective_timeout)

    def run_script(
        self,
        conn: ManagedConnection,
        script: str,
        *,
        confirm_destructive: bool = False,
    ) -> dict[str, Any]:
        """Execute an arbitrary SQL script (write mode, MCP-4).

        Policy: ``mcp.allow_writes`` must be enabled in the connection file;
        destructive/unparseable statements additionally require
        ``confirm_destructive=True`` from the caller.
        """
        settings = conn.cfg.mcp_settings
        verdict = classify_script(script, dialect=dialect_for(conn.cfg.type))
        if not settings.allow_writes:
            raise MCPPermissionError(
                "run_script запрещён для этого подключения: mcp.allow_writes=false "
                "(файл подключения, блок mcp:)"
            )
        if verdict.requires_destructive_confirm and not confirm_destructive:
            raise MCPPermissionError(
                f"Скрипт содержит опасные операторы ({verdict.worst.value}). "
                f"Детали: {verdict.summary()}. "
                "Повторите вызов с confirm_destructive=true, если это осознанно."
            )
        started = time.perf_counter()
        conn.adapter.execute_script(script)
        duration_ms = int((time.perf_counter() - started) * 1000)
        logger.info(f"MCP run_script: стейтментов={len(verdict.statements)}, {duration_ms} мс")
        return {
            "status": "ok",
            "duration_ms": duration_ms,
            "statements": len(verdict.statements),
            "worst_class": verdict.worst.value,
        }


# --- structure filtering (pure functions, unit-testable without a DB) ---

def extract_schemas(structure: dict[str, Any]) -> list[str]:
    """Schema names from a get_database_structure() dump."""
    return [s["name"] for s in structure.get("schemas", [])]


def extract_objects(structure: dict[str, Any], schema: str, object_type: str) -> list[dict[str, Any]]:
    """Compact object list (name + comment) of one type within one schema."""
    key = OBJECT_TYPE_KEYS.get(object_type)
    if key is None:
        raise DatabaseError(
            f"Неизвестный тип объекта {object_type!r}. Допустимо: {', '.join(sorted(OBJECT_TYPE_KEYS))}."
        )
    for entry in structure.get("schemas", []):
        if entry.get("name") == schema:
            return [{"name": o.get("name"), "comment": o.get("comment")} for o in entry.get(key, [])]
    raise DatabaseError(f"Схема {schema!r} не найдена в структуре базы.")


def extract_object_details(
    structure: dict[str, Any], schema: str, object_name: str, object_type: str
) -> dict[str, Any]:
    """Full structure entry for one object, or raise when missing."""
    key = OBJECT_TYPE_KEYS.get(object_type)
    if key is None:
        raise DatabaseError(
            f"Неизвестный тип объекта {object_type!r}. Допустимо: {', '.join(sorted(OBJECT_TYPE_KEYS))}."
        )
    for entry in structure.get("schemas", []):
        if entry.get("name") != schema:
            continue
        for obj in entry.get(key, []):
            if obj.get("name") == object_name:
                return obj
        raise DatabaseError(
            f"Объект {object_type} {schema}.{object_name} не найден. "
            f"Проверьте список: list_objects(schema={schema!r}, object_type={object_type!r})."
        )
    raise DatabaseError(f"Схема {schema!r} не найдена в структуре базы.")

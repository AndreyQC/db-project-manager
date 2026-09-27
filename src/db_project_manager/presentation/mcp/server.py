"""FastMCP server assembly (Phase 19, MCP-1).

Registers thin wrappers around :class:`MCPToolBox` methods. Everything the
LLM sees — names, descriptions (docstrings), parameter docs — lives here;
all behavior lives in the tool box (unit-testable without MCP).

Import of the ``mcp`` package happens here, so the rest of the project does
not require the ``[mcp]`` extra (``uv sync --extra mcp``).
"""

from __future__ import annotations

from typing import Any

from db_project_manager.application.mcp_service import ConnectionManager
from db_project_manager.infrastructure.config.app_config import CFG


def create_server(manager: ConnectionManager, cfg: CFG) -> Any:
    """Build the FastMCP instance with all db-pm tools registered."""
    # Imported lazily: keeps db-pm usable without the [mcp] extra.
    from mcp.server.fastmcp import FastMCP
    from mcp.types import ToolAnnotations

    from db_project_manager.presentation.mcp.tools import MCPToolBox

    box = MCPToolBox(manager, cfg)

    read_only = ToolAnnotations(readOnlyHint=True)
    write = ToolAnnotations(destructiveHint=False)
    destructive = ToolAnnotations(destructiveHint=True)

    mcp: Any = FastMCP(
        "db-pm",
        instructions=(
            "db-pm — работа со схемами БД и данными через именованные подключения "
            "(connections/*.yaml). Подключение передаётся параметром connection. "
            "По умолчанию доступ только на чтение (query/explain/инспекция); "
            "run_script и деплой включаются в файле подключения блоком mcp:. "
            "Всегда квалифицируйте идентификаторы (schema.name) — search_path не гарантирован."
        ),
    )

    @mcp.tool(name="list_connections", annotations=read_only)
    def list_connections() -> list[dict[str, Any]]:
        """Список доступных подключений: имя, СУБД, host/БД/user и эффективные права MCP
        (allow_writes, allow_deploy, row_limit, query_timeout_s). Секретов не возвращает.
        Начинайте знакомство с БД отсюда: connection = точное имя подключения."""
        return box.list_connections()

    @mcp.tool(name="list_schemas", annotations=read_only)
    def list_schemas(connection: str) -> list[str]:
        """Пользовательские схемы БД выбранного подключения (системные исключены).
        Первый шаг интроспекции: схемы → list_objects → get_object_details."""
        return box.list_schemas(connection)

    @mcp.tool(name="list_objects", annotations=read_only)
    def list_objects(connection: str, schema: str, object_type: str = "table") -> list[dict[str, Any]]:
        """Объекты одной схемы: имя + комментарий.
        object_type: table | view | materialized_view | sequence | function | procedure.
        Пример: list_objects(connection="local", schema="public", object_type="table")."""
        return box.list_objects(connection, schema, object_type)

    @mcp.tool(name="get_object_details", annotations=read_only)
    def get_object_details(
        connection: str, schema: str, object_name: str, object_type: str = "table"
    ) -> dict[str, Any]:
        """Полное описание объекта: для таблицы — колонки/типы/констрейнты/индексы,
        для функций — тело и сигнатура. object_type как в list_objects."""
        return box.get_object_details(connection, schema, object_name, object_type)

    @mcp.tool(name="query", annotations=read_only)
    def query(
        connection: str, sql: str, max_rows: int | None = None, timeout_s: int | None = None
    ) -> Any:
        """Выполнить ОДИН read-only стейтмент (SELECT/WITH/SHOW) и получить строки
        (columns, rows, row_count, truncated). Лимит строк и таймаут не выше настроек
        подключения. Для изменений данных — run_script. Пример:
        query(connection="local", sql="SELECT count(*) FROM public.bookings")."""
        return box.query(connection, sql, max_rows=max_rows, timeout_s=timeout_s)

    @mcp.tool(name="explain", annotations=read_only)
    def explain(connection: str, sql: str, analyze: bool = False, fmt: str = "auto") -> Any:
        """План выполнения стейтмента (EXPLAIN). fmt: auto (json с fallback на text) | text | json.
        analyze=true ИСПОЛНЯЕТ запрос — разрешён только для read-only стейтментов.
        Для медленных запросов сначала get_top_queries, потом explain."""
        return box.explain(connection, sql, analyze=analyze, fmt=fmt)

    @mcp.tool(name="get_top_queries", annotations=read_only)
    def get_top_queries(connection: str, sort_by: str = "resources", limit: int = 10) -> list[dict[str, Any]]:
        """Самые тяжёлые запросы из pg_stat_statements (query, calls, total_ms, mean_ms, rows).
        sort_by: resources | total | mean. Требует установленного расширения pg_stat_statements."""
        return box.get_top_queries(connection, sort_by=sort_by, limit=limit)

    @mcp.tool(name="run_script", annotations=write)
    def run_script(connection: str, script: str, confirm_destructive: bool = False) -> dict[str, Any]:
        """Исполнить SQL-скрипт (любые операторы, AUTOCOMMIT, скрипт целиком).
        Гейты: mcp.allow_writes=true в файле подключения; при наличии DROP/TRUNCATE
        или нераспознанных операторов — повторный вызов с confirm_destructive=true."""
        return box.run_script(connection, script, confirm_destructive=confirm_destructive)

    @mcp.tool(name="deploy_plan", annotations=read_only)
    def deploy_plan(
        connection: str, codebase_dir: str = ".", include_drops: bool = False, output_dir: str | None = None
    ) -> dict[str, Any]:
        """Dry-run деплоя кодовой базы: safety gate + ALTER-план + артефакты
        (delta/*.sql, plan.json, plan.md). Read-only, ничего не применяется.
        codebase_dir — корень кодовой базы (manifest.yaml + дерево SQL)."""
        return box.deploy_plan(connection, codebase_dir=codebase_dir, include_drops=include_drops, output_dir=output_dir)

    @mcp.tool(name="deploy_analyze", annotations=read_only)
    def deploy_analyze(
        connection: str, codebase_dir: str = ".", output_dir: str | None = None
    ) -> dict[str, Any]:
        """Safety gate (dry-run): сравнение кодовой базы с живой БД, оценка наличия данных
        в тронутых таблицах, покрытие pre-скриптами. Read-only; отчёт safety_gate_report.md."""
        return box.deploy_analyze(connection, codebase_dir=codebase_dir, output_dir=output_dir)

    @mcp.tool(name="deploy_apply", annotations=destructive)
    def deploy_apply(
        connection: str,
        codebase_dir: str = ".",
        include_drops: bool = False,
        rehearsal: bool = True,
        keep_rehearsal_db: bool = False,
        output_dir: str | None = None,
    ) -> dict[str, Any]:
        """ПРИМЕНЯЕТ деплой к БД: safety gate → pre-скрипты → дельта → post-скрипты.
        По умолчанию полная репетиция на temp-БД; провал репетиции не трогает target.
        Требует mcp.allow_deploy=true в файле подключения. Может выполняться долго."""
        return box.deploy_apply(
            connection,
            codebase_dir=codebase_dir,
            include_drops=include_drops,
            rehearsal=rehearsal,
            keep_rehearsal_db=keep_rehearsal_db,
            output_dir=output_dir,
        )

    @mcp.tool(name="deploy_reset", annotations=destructive)
    def deploy_reset(
        connection: str,
        codebase_dir: str = ".",
        confirm_database: str = "",
        dry_run: bool = False,
        output_dir: str | None = None,
    ) -> dict[str, Any]:
        """DESTRUCTIVE: сброс ВСЕХ пользовательских схем БД (deploy reset).
        Гейты: mcp.allow_deploy=true, allow_drop_schemas=true в файле подключения и
        confirm_database='<точное имя БД>'. dry_run=true — только план и артефакты.
        Сначала вызовите с dry_run=true и покажите план пользователю."""
        return box.deploy_reset(
            connection,
            codebase_dir=codebase_dir,
            confirm_database=confirm_database,
            dry_run=dry_run,
            output_dir=output_dir,
        )

    return mcp


def run_server(connections_dir: str, config: Any = None) -> None:
    """Configure logging (stderr!) and serve stdio until the client disconnects."""
    from db_project_manager.infrastructure.config.app_config import load_cfg
    from db_project_manager.infrastructure.logging_setup import configure as configure_logging

    cfg = load_cfg(config)
    # stdout carries the MCP protocol; loguru's console sink is stderr-based.
    configure_logging(level=cfg.logging.level, console=True, logs_dir=cfg.paths.logs_dir, file_name="mcp.log")

    manager = ConnectionManager(connections_dir)
    mcp = create_server(manager, cfg)
    try:
        mcp.run(transport="stdio")
    finally:
        manager.disconnect_all()

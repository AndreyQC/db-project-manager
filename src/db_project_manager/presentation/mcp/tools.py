"""MCP tool implementations (Phase 19).

:class:`MCPToolBox` holds the logic of every MCP tool as plain methods —
unit-testable without the MCP runtime. ``server.py`` only registers thin
FastMCP wrappers around these; docstrings double as LLM-facing descriptions.

Deployment tools reuse the exact CLI wiring (services + ``create_run_dir`` +
progress callbacks) so an MCP-driven deploy behaves identically to
``db-pm deploy`` and cannot bypass its safety machinery.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from loguru import logger

from db_project_manager.application.deploy_apply_service import (
    DeployApplyError,
    DeployApplyRejected,
    DeployApplyService,
)
from db_project_manager.application.mcp_service import (
    MCPPermissionError,
    MCPQueryService,
    ConnectionManager,
    extract_object_details,
    extract_objects,
)
from db_project_manager.application.profiling_service import ProfilingService
from db_project_manager.application.safety_gate_service import SafetyGateError, SafetyGateService
from db_project_manager.application.schema_reset_service import (
    SchemaResetError,
    SchemaResetRejected,
    SchemaResetService,
)
from db_project_manager.domain.connection import ConnectionConfig
from db_project_manager.domain.query import ExplainResult, QueryResult
from db_project_manager.infrastructure.config.app_config import CFG
from db_project_manager.infrastructure.files.run_naming import create_run_dir
from db_project_manager.infrastructure.query_log import log_db_call

#: How many progress lines travel back to the LLM (deploy runs are long).
PROGRESS_TAIL = 50


class ProgressLog:
    """Collects service progress lines; returns the tail for the tool answer."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def __call__(self, message: str, current: int, total: int) -> None:
        line = f"[{current}/{total}] {message}" if total else message
        self.lines.append(line)

    def tail(self) -> list[str]:
        return self.lines[-PROGRESS_TAIL:]


class MCPToolBox:
    """All db-pm MCP tools (Phase 19, MCP-1..MCP-8)."""

    def __init__(self, manager: ConnectionManager, cfg: CFG) -> None:
        self.manager = manager
        self.cfg = cfg
        self.queries = MCPQueryService()
        self.profiles = ProfilingService(manager=manager)

    # --- introspection (read-only, MCP-5) ---

    def list_connections(self) -> list[dict[str, Any]]:
        """Доступные подключения (имена, БД, эффективные права MCP). Без секретов."""
        return self.manager.list_connections()

    def list_schemas(self, connection: str) -> list[str]:
        """Пользовательские схемы выбранного подключения."""
        with self.manager.connection(connection) as conn:
            return conn.adapter.list_schemas()

    def list_objects(self, connection: str, schema: str, object_type: str = "table") -> list[dict[str, Any]]:
        """Объекты схемы: имя + комментарий. Типы: table|view|materialized_view|sequence|function|procedure."""
        with self.manager.connection(connection) as conn:
            structure = conn.adapter.get_database_structure()
        return extract_objects(structure, schema, object_type)

    def get_object_details(
        self, connection: str, schema: str, object_name: str, object_type: str = "table"
    ) -> dict[str, Any]:
        """Полная структура объекта: колонки, индексы, констрейнты, DDL-body."""
        with self.manager.connection(connection) as conn:
            structure = conn.adapter.get_database_structure()
        return extract_object_details(structure, schema, object_name, object_type)

    # --- data & plans (read-only, MCP-2/3) ---

    @staticmethod
    def _result_to_jsonable(result: Any) -> Any:
        """Full DB response for the query log (jsonb values are already JSON-safe)."""
        if isinstance(result, (QueryResult, ExplainResult)):
            return result.model_dump()
        return result

    def _logged(self, tool: str, connection: str, sql: str | None, call) -> Any:
        """Run a DB call, emitting its SQL text and full response to the query log.

        Refusals and errors are logged too (``error`` field) — the log is the
        complete audit trail of what the LLM asked the database.
        """
        started = time.perf_counter()
        try:
            result = call()
        except Exception as e:
            log_db_call(
                {
                    "tool": tool,
                    "connection": connection,
                    "sql": sql,
                    "error": f"{type(e).__name__}: {e}",
                    "duration_ms": int((time.perf_counter() - started) * 1000),
                }
            )
            raise
        event: dict[str, Any] = {
            "tool": tool,
            "connection": connection,
            "sql": sql,
            "duration_ms": int((time.perf_counter() - started) * 1000),
            "response": self._result_to_jsonable(result),
        }
        if isinstance(result, QueryResult):
            event["row_count"] = result.row_count
            event["truncated"] = result.truncated
        if isinstance(result, ExplainResult):
            event["fmt"] = result.fmt
            event["analyzed"] = result.analyzed
        log_db_call(event)
        return result

    def query(
        self,
        connection: str,
        sql: str,
        max_rows: int | None = None,
        timeout_s: int | None = None,
    ) -> QueryResult:
        """Read-only SELECT (один стейтмент) с лимитом строк и таймаутом."""

        def call() -> QueryResult:
            with self.manager.connection(connection) as conn:
                return self.queries.query(conn, sql, max_rows=max_rows, timeout_s=timeout_s)

        return self._logged("query", connection, sql, call)

    def explain(
        self,
        connection: str,
        sql: str,
        analyze: bool = False,
        fmt: str = "auto",
    ) -> ExplainResult:
        """План выполнения стейтмента. analyze=true исполняет запрос (только read-only)."""

        def call() -> ExplainResult:
            with self.manager.connection(connection) as conn:
                return self.queries.explain(conn, sql, analyze=analyze, fmt=fmt)

        return self._logged("explain", connection, sql, call)

    def get_top_queries(self, connection: str, sort_by: str = "resources", limit: int = 10) -> list[dict[str, Any]]:
        """Топ запросов по pg_stat_statements. sort_by: resources|total|mean."""

        def call() -> list[dict[str, Any]]:
            with self.manager.connection(connection) as conn:
                return conn.adapter.get_top_queries(sort_by=sort_by, limit=limit)

        return self._logged("get_top_queries", connection, None, call)

    def profile_tables(self, connection: str, tables: list[str]) -> list[dict[str, Any]]:
        """Профайлинг таблиц (read-only). Требует profiling.enabled=true в блоке
        profiling: файла подключения; ANALYZE не запускается."""

        def call() -> list[dict[str, Any]]:
            return [tp.model_dump() for tp in self.profiles.profile_tables(connection, tables)]

        return self._logged("profile_tables", connection, None, call)

    # --- scripts (mutating, MCP-4) ---

    def run_script(
        self, connection: str, script: str, confirm_destructive: bool = False
    ) -> dict[str, Any]:
        """Исполнить SQL-скрипт. Требует mcp.allow_writes; DROP/TRUNCATE — ещё и confirm_destructive=true."""

        def call() -> dict[str, Any]:
            with self.manager.connection(connection) as conn:
                return self.queries.run_script(conn, script, confirm_destructive=confirm_destructive)

        return self._logged("run_script", connection, script, call)

    # --- deploy pipeline (MCP-6) ---

    def _deploy_run_dir(self, output_dir: str | None) -> Path:
        return create_run_dir(output_dir or self.cfg.paths.default_output_dir)

    def _require_deploy_permission(self, cfg_conn: ConnectionConfig, *, mutating: bool) -> None:
        if mutating and not cfg_conn.mcp_settings.allow_deploy:
            raise MCPPermissionError(
                "deploy_apply/deploy_reset запрещены для этого подключения: "
                "mcp.allow_deploy=false (файл подключения, блок mcp:)"
            )

    def deploy_plan(
        self,
        connection: str,
        codebase_dir: str = ".",
        include_drops: bool = False,
        output_dir: str | None = None,
    ) -> dict[str, Any]:
        """Dry-run деплоя: safety gate + ALTER-план + артефакты (plan.md). Read-only."""
        cfg_conn = self.manager.load_config(connection)
        run_dir = self._deploy_run_dir(output_dir)
        progress = ProgressLog()
        service = DeployApplyService(service_schema=self.cfg.deploy.service_schema)
        try:
            plan = service.plan(
                Path(codebase_dir), cfg_conn, run_dir,
                include_drops=include_drops, progress=progress,
            )
        except DeployApplyRejected as e:
            return {"status": "rejected", "error": str(e), "artifacts_dir": str(run_dir)}
        except DeployApplyError as e:
            return {"status": "error", "error": str(e), "artifacts_dir": str(run_dir)}
        return {
            "status": "ok" if not plan.violations else "blocked",
            "operations": len(plan.operations),
            "safe": len(plan.safe_ops),
            "needs_pre": len(plan.needs_pre_ops),
            "blocked": len(plan.violations),
            "report": str(run_dir / "plan.md"),
            "progress_tail": progress.tail(),
        }

    def deploy_analyze(
        self,
        connection: str,
        codebase_dir: str = ".",
        output_dir: str | None = None,
    ) -> dict[str, Any]:
        """Safety gate (dry-run): код ↔ БД с данными. Read-only, ничего не применяется."""
        cfg_conn = self.manager.load_config(connection)
        run_dir = self._deploy_run_dir(output_dir)
        progress = ProgressLog()
        service = SafetyGateService(service_schema=self.cfg.deploy.service_schema)
        try:
            verdict = service.analyze(Path(codebase_dir), cfg_conn, run_dir, progress=progress)
        except SafetyGateError as e:
            return {"status": "error", "error": str(e), "artifacts_dir": str(run_dir)}
        result: dict[str, Any] = {
            "status": "clean" if verdict.clean else "violations",
            "touched_tables": len(verdict.touched),
            "violations": [
                {
                    "table": f"{v.object_schema}.{v.name}",
                    "touch": v.touch.value,
                    "estimated_rows": v.estimated_rows,
                }
                for v in verdict.violations
            ],
            "report": str(run_dir / "safety_gate_report.md"),
            "progress_tail": progress.tail(),
        }
        if verdict.ignored_build_false:
            result["ignored_build_false"] = verdict.ignored_build_false
        return result

    def deploy_apply(
        self,
        connection: str,
        codebase_dir: str = ".",
        include_drops: bool = False,
        rehearsal: bool = True,
        keep_rehearsal_db: bool = False,
        output_dir: str | None = None,
    ) -> dict[str, Any]:
        """DESTRUCTIVE: применить деплой (rehearsal на temp-БД → target). Требует mcp.allow_deploy."""
        cfg_conn = self.manager.load_config(connection)
        self._require_deploy_permission(cfg_conn, mutating=True)
        run_dir = self._deploy_run_dir(output_dir)
        progress = ProgressLog()
        service = DeployApplyService(service_schema=self.cfg.deploy.service_schema)
        try:
            result = service.apply(
                Path(codebase_dir), cfg_conn, run_dir,
                include_drops=include_drops,
                rehearsal=rehearsal,
                keep_rehearsal_db=keep_rehearsal_db,
                progress=progress,
            )
        except DeployApplyRejected as e:
            return {"status": "rejected", "error": str(e), "artifacts_dir": str(run_dir)}
        except DeployApplyError as e:
            return {"status": "error", "error": str(e), "artifacts_dir": str(run_dir)}
        return {
            "status": "ok",
            "applied": result.applied,
            "planned": result.planned,
            "applied_version": result.applied_version,
            "rehearsal_db": result.rehearsal_db,
            "artifacts_dir": str(result.output_dir),
            "progress_tail": progress.tail(),
        }

    def deploy_reset(
        self,
        connection: str,
        codebase_dir: str = ".",
        confirm_database: str = "",
        dry_run: bool = False,
        output_dir: str | None = None,
    ) -> dict[str, Any]:
        """DESTRUCTIVE: сброс пользовательских схем. Требует mcp.allow_deploy,
        allow_drop_schemas в файле подключения и confirm_database = точное имя БД."""
        cfg_conn = self.manager.load_config(connection)
        self._require_deploy_permission(cfg_conn, mutating=True)
        if not dry_run and confirm_database != cfg_conn.database:
            raise MCPPermissionError(
                f"Подтвердите сброс: передайте confirm_database='{cfg_conn.database}' "
                "(точное имя целевой БД, как в CLI-подтверждении)."
            )
        run_dir = self._deploy_run_dir(output_dir)
        progress = ProgressLog()
        service = SchemaResetService(service_schema=self.cfg.deploy.service_schema)
        try:
            plan = service.collect(Path(codebase_dir), cfg_conn)
            plan_summary = [
                {
                    "schema": s,
                    "mode": "content-drop" if plan.is_content_drop(s) else "drop_schema",
                    "objects": plan.object_counts.get(s, 0),
                    "in_codebase": s in plan.in_codebase,
                }
                for s in plan.schemas
            ]
            result = service.execute(plan, run_dir, dry_run=dry_run, progress=progress)
        except SchemaResetRejected as e:
            return {"status": "rejected", "error": str(e)}
        except SchemaResetError as e:
            return {"status": "error", "error": str(e), "artifacts_dir": str(run_dir)}
        logger.info(f"MCP deploy_reset: dry_run={dry_run}, connection={connection}")
        return {
            "status": "ok",
            "dry_run": result.dry_run,
            "schemas_wiped": len(result.schemas_wiped),
            "schemas_dropped": len(result.schemas_dropped),
            "extensions_dropped": len(result.extensions_dropped),
            "journal_truncated": result.journal_truncated,
            "plan": plan_summary,
            "report": str(run_dir / "reset_report.md"),
            "progress_tail": progress.tail(),
        }

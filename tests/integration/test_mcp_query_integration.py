"""Integration tests for the MCP adapter surface (Phase 19, MCP-2/3).

Real PostgreSQL 16 in Docker: run_query (rows, truncation, read-only
transaction backstop, statement timeout), explain (text/json/analyze) and
get_top_queries graceful degradation without pg_stat_statements.
"""

from __future__ import annotations

import pytest

from db_project_manager.application.mcp_service import (
    MCPPermissionError,
    MCPQueryService,
    ManagedConnection,
)
from db_project_manager.domain.connection import McpSettings
from db_project_manager.infrastructure.database.base import DatabaseError, NotSupportedError
from db_project_manager.infrastructure.database.registry import get_adapter

pytestmark = pytest.mark.integration


@pytest.fixture()
def adapter(pg_conn_cfg):
    adapter = get_adapter(pg_conn_cfg)
    adapter.connect(pg_conn_cfg)
    try:
        adapter.execute_script(
            "CREATE TABLE public.items (id int PRIMARY KEY, name text); "
            "INSERT INTO public.items SELECT g, 'item' || g FROM generate_series(1, 10) g;"
        )
        yield adapter
    finally:
        adapter.disconnect()


def test_run_query_returns_columns_and_rows(adapter):
    result = adapter.run_query("SELECT id, name FROM public.items ORDER BY id", max_rows=5)
    assert result.columns == ["id", "name"]
    assert result.row_count == 5
    assert result.rows[0] == {"id": 1, "name": "item1"}
    assert result.truncated is False


def test_run_query_truncates_beyond_max_rows(adapter):
    result = adapter.run_query("SELECT id FROM public.items", max_rows=3)
    assert result.row_count == 3
    assert result.truncated is True


def test_run_query_readonly_transaction_rejects_writes(adapter):
    with pytest.raises(DatabaseError, match="read-only"):
        adapter.run_query("CREATE TABLE public.hack (x int)", readonly=True)


def test_run_query_readonly_rejects_data_changes(adapter):
    with pytest.raises(DatabaseError, match="read-only"):
        adapter.run_query("DELETE FROM public.items", readonly=True)


def test_run_query_statement_timeout(adapter):
    with pytest.raises(DatabaseError, match="statement_timeout"):
        adapter.run_query("SELECT pg_sleep(10)", timeout_s=1)


def test_run_query_json_value_serialization(adapter):
    adapter.execute_script(
        "CREATE TABLE public.payload (doc jsonb, amount numeric(10,2));"
        "INSERT INTO public.payload VALUES ('{\"a\": 1}'::jsonb, 12.50);"
    )
    result = adapter.run_query("SELECT doc, amount FROM public.payload")
    assert result.rows[0] == {"doc": {"a": 1}, "amount": "12.50"}


def test_explain_text(adapter):
    result = adapter.explain("SELECT * FROM public.items WHERE id = 1", fmt="text")
    assert result.fmt == "text"
    assert "Seq Scan" in result.plan or "Index Scan" in result.plan


def test_explain_json_auto(adapter):
    result = adapter.explain("SELECT * FROM public.items WHERE id = 1")
    assert result.fmt == "json"
    assert isinstance(result.plan, dict)
    assert "Plan" in result.plan


def test_explain_analyze_read_only_executes(adapter):
    result = adapter.explain("SELECT count(*) FROM public.items", analyze=True)
    assert result.analyzed is True
    assert "Actual" in str(result.plan) or "Actual" in str(result.plan.get("Plan", {}))


def test_explain_analyze_write_blocked_by_readonly_tx(adapter):
    # Adapter-level backstop: EXPLAIN ANALYZE of a DELETE inside a READ ONLY
    # transaction is refused by the server itself.
    with pytest.raises(DatabaseError, match="read-only"):
        adapter.explain("DELETE FROM public.items", analyze=True)


def test_get_top_queries_without_extension_is_not_supported(adapter):
    with pytest.raises(NotSupportedError, match="pg_stat_statements"):
        adapter.get_top_queries()


# --- service-level policy over the real adapter (MCP-2/4) ---


@pytest.fixture()
def managed(pg_conn_cfg):
    cfg = pg_conn_cfg.model_copy(update={"mcp": McpSettings(allow_writes=True, row_limit=5)})
    adapter = get_adapter(cfg)
    adapter.connect(cfg)
    try:
        yield ManagedConnection(cfg=cfg, adapter=adapter)
    finally:
        adapter.disconnect()


def test_service_query_over_real_db(managed):
    result = MCPQueryService().query(managed, "SELECT count(*) AS n FROM public.items")
    assert result.rows[0]["n"] == 10


def test_service_explain_analyze_write_refused(managed):
    with pytest.raises(MCPPermissionError, match="read-only"):
        MCPQueryService().explain(managed, "DELETE FROM public.items", analyze=True)


def test_service_run_script_destructive_confirm_flow(managed):
    service = MCPQueryService()
    with pytest.raises(MCPPermissionError, match="confirm_destructive"):
        service.run_script(managed, "DROP TABLE public.items")
    result = service.run_script(managed, "DROP TABLE public.items", confirm_destructive=True)
    assert result["status"] == "ok"
    # The table is gone — verify via the same adapter.
    after = MCPQueryService().query(
        managed, "SELECT count(*) AS n FROM information_schema.tables WHERE table_name = 'items'"
    )
    assert after.rows[0]["n"] == 0

"""Tests for MCP application services (Phase 19, MCP-2..5/7)."""

from __future__ import annotations

from typing import Any

import pytest

from db_project_manager.application.mcp_service import (
    MCPPermissionError,
    MCPQueryService,
    ManagedConnection,
    extract_object_details,
    extract_objects,
    extract_schemas,
    ConnectionManager,
)
from db_project_manager.domain.connection import ConnectionConfig, McpSettings
from db_project_manager.domain.query import ExplainResult, QueryResult
from db_project_manager.infrastructure.database.base import DatabaseError

from tests.unit.test_deploy_service import DeployFakeAdapter


class RecordingAdapter(DeployFakeAdapter):
    """DeployFakeAdapter + call recording for the MCP surface."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.run_query_calls: list[dict[str, Any]] = []
        self.explain_calls: list[dict[str, Any]] = []
        self.executed_scripts: list[str] = []
        self.disconnected = False

    def disconnect(self) -> None:
        self.disconnected = True

    def run_query(self, sql, *, max_rows=50, timeout_s=60, readonly=True):
        self.run_query_calls.append(
            {"sql": sql, "max_rows": max_rows, "timeout_s": timeout_s, "readonly": readonly}
        )
        return QueryResult(columns=["x"], rows=[{"x": 1}], row_count=1)

    def explain(self, sql, *, analyze=False, fmt="auto", timeout_s=60):
        self.explain_calls.append({"sql": sql, "analyze": analyze, "fmt": fmt})
        return ExplainResult(fmt="text", plan="Seq Scan", analyzed=analyze)

    def execute_script(self, script: str) -> None:
        self.executed_scripts.append(script)


def make_conn(**mcp_kwargs: Any) -> tuple[ManagedConnection, RecordingAdapter]:
    cfg = ConnectionConfig(
        host="localhost",
        database="db",
        username="u",
        mcp=McpSettings(**mcp_kwargs) if mcp_kwargs else None,
    )
    adapter = RecordingAdapter()
    return ManagedConnection(cfg=cfg, adapter=adapter), adapter


# --- query policy (MCP-2) ---


def test_query_read_only_passes():
    conn, adapter = make_conn()
    result = MCPQueryService().query(conn, "SELECT 1")
    assert result.row_count == 1
    assert adapter.run_query_calls[0]["readonly"] is True


def test_query_write_rejected():
    conn, adapter = make_conn(allow_writes=True)
    with pytest.raises(MCPPermissionError, match="read-only"):
        MCPQueryService().query(conn, "DELETE FROM t")
    assert adapter.run_query_calls == []


def test_query_multi_statement_rejected():
    conn, _ = make_conn()
    with pytest.raises(MCPPermissionError, match="ровно один"):
        MCPQueryService().query(conn, "SELECT 1; SELECT 2")


def test_query_denylisted_function_rejected():
    conn, _ = make_conn()
    with pytest.raises(MCPPermissionError, match="dblink"):
        MCPQueryService().query(conn, "SELECT dblink('x', 'y')")


def test_query_limits_capped_by_connection_settings():
    conn, adapter = make_conn(row_limit=10, query_timeout_s=15)
    MCPQueryService().query(conn, "SELECT 1", max_rows=500, timeout_s=999)
    call = adapter.run_query_calls[0]
    assert call["max_rows"] == 10
    assert call["timeout_s"] == 15


def test_query_defaults_from_connection_settings():
    conn, adapter = make_conn(row_limit=7, query_timeout_s=30)
    MCPQueryService().query(conn, "SELECT 1")
    call = adapter.run_query_calls[0]
    assert call["max_rows"] == 7
    assert call["timeout_s"] == 30


# --- explain policy (MCP-3) ---


def test_explain_plain_allowed_for_any_statement():
    conn, adapter = make_conn()
    MCPQueryService().explain(conn, "UPDATE t SET x = 1")
    assert adapter.explain_calls[0]["analyze"] is False


def test_explain_analyze_read_only_passes():
    conn, adapter = make_conn()
    MCPQueryService().explain(conn, "SELECT * FROM t", analyze=True)
    assert adapter.explain_calls[0]["analyze"] is True


def test_explain_analyze_write_rejected():
    conn, adapter = make_conn(allow_writes=True)
    with pytest.raises(MCPPermissionError, match="read-only"):
        MCPQueryService().explain(conn, "DELETE FROM t", analyze=True)
    assert adapter.explain_calls == []


# --- run_script policy (MCP-4) ---


def test_run_script_denied_without_allow_writes():
    conn, adapter = make_conn()
    with pytest.raises(MCPPermissionError, match="allow_writes"):
        MCPQueryService().run_script(conn, "INSERT INTO t VALUES (1)")
    assert adapter.executed_scripts == []


def test_run_script_write_allowed_with_flag():
    conn, adapter = make_conn(allow_writes=True)
    result = MCPQueryService().run_script(conn, "INSERT INTO t VALUES (1)")
    assert result["status"] == "ok"
    assert adapter.executed_scripts == ["INSERT INTO t VALUES (1)"]


def test_run_script_destructive_requires_confirm():
    conn, adapter = make_conn(allow_writes=True)
    with pytest.raises(MCPPermissionError, match="confirm_destructive"):
        MCPQueryService().run_script(conn, "DROP TABLE t")
    assert adapter.executed_scripts == []


def test_run_script_destructive_with_confirm_executes():
    conn, adapter = make_conn(allow_writes=True)
    result = MCPQueryService().run_script(conn, "DROP TABLE t", confirm_destructive=True)
    assert result["worst_class"] == "destructive"
    assert adapter.executed_scripts == ["DROP TABLE t"]


def test_run_script_unparseable_requires_confirm():
    conn, _ = make_conn(allow_writes=True)
    with pytest.raises(MCPPermissionError, match="confirm_destructive"):
        MCPQueryService().run_script(conn, "CREATE OR REPLACE @@garbage")


# --- structure filtering (MCP-5) ---


STRUCTURE = {
    "schemas": [
        {
            "name": "public",
            "comment": None,
            "tables": [
                {"name": "bookings", "comment": "Бронирования", "columns": [{"name": "id"}]},
                {"name": "flights", "comment": None, "columns": []},
            ],
            "views": [{"name": "v_report", "comment": None}],
        },
        {"name": "audit", "comment": None, "tables": [{"name": "log", "comment": None}]},
    ]
}


def test_extract_schemas():
    assert extract_schemas(STRUCTURE) == ["public", "audit"]


def test_extract_objects_filters_schema_and_type():
    objects = extract_objects(STRUCTURE, "public", "table")
    assert [o["name"] for o in objects] == ["bookings", "flights"]
    assert objects[0]["comment"] == "Бронирования"


def test_extract_objects_unknown_type_rejected():
    with pytest.raises(DatabaseError, match="Неизвестный тип"):
        extract_objects(STRUCTURE, "public", "blob")


def test_extract_objects_unknown_schema_rejected():
    with pytest.raises(DatabaseError, match="не найдена"):
        extract_objects(STRUCTURE, "nope", "table")


def test_extract_object_details():
    details = extract_object_details(STRUCTURE, "public", "bookings", "table")
    assert details["columns"] == [{"name": "id"}]


def test_extract_object_details_missing_object():
    with pytest.raises(DatabaseError, match="не найден"):
        extract_object_details(STRUCTURE, "public", "ghost", "table")


# --- ConnectionManager (MCP-1/7) ---


def _write_connection(dir_path, name: str, mcp_block: str = "") -> None:
    import yaml

    data = yaml.safe_load(
        "host: localhost\nport: 5432\ndatabase: db\nusername: u\n"
        f"password: plain\ntype: postgres\n{mcp_block}"
    )
    (dir_path / f"{name}.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")


def test_manager_list_connections_reports_permissions(tmp_path):
    _write_connection(tmp_path, "ro_conn")
    _write_connection(tmp_path, "rw_conn", "mcp:\n  allow_writes: true\n")
    manager = ConnectionManager(tmp_path)
    listed = {c["name"]: c for c in manager.list_connections()}
    assert listed["ro_conn"]["mcp"]["allow_writes"] is False
    assert listed["rw_conn"]["mcp"]["allow_writes"] is True
    # no credential leakage
    assert "password" not in listed["ro_conn"]
    assert "password" not in listed["rw_conn"]


def test_manager_connects_once_and_caches(tmp_path):
    _write_connection(tmp_path, "conn")
    created: list[RecordingAdapter] = []

    def factory(cfg):
        adapter = RecordingAdapter()
        created.append(adapter)
        return adapter

    manager = ConnectionManager(tmp_path, adapter_factory=factory)
    with manager.connection("conn") as first:
        pass
    with manager.connection("conn") as second:
        pass
    assert len(created) == 1
    assert first is second


def test_manager_disconnect_all(tmp_path):
    _write_connection(tmp_path, "conn")
    adapters: list[RecordingAdapter] = []

    def factory(cfg):
        adapter = RecordingAdapter()
        adapters.append(adapter)
        return adapter

    manager = ConnectionManager(tmp_path, adapter_factory=factory)
    with manager.connection("conn"):
        pass
    manager.disconnect_all()
    assert adapters[0].disconnected is True

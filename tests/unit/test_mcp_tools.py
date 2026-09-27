"""Tests for MCP tool implementations (Phase 19, MCP-1..6)."""

from __future__ import annotations

from typing import Any

import pytest

from db_project_manager.application.mcp_service import MCPPermissionError
from db_project_manager.infrastructure.config.app_config import CFG, PathsConfig
from db_project_manager.presentation.mcp import tools as tools_module
from db_project_manager.presentation.mcp.tools import MCPToolBox

from tests.unit.test_mcp_service import RecordingAdapter, _write_connection


@pytest.fixture()
def connections(tmp_path):
    _write_connection(tmp_path, "ro_conn")
    _write_connection(tmp_path, "deploy_conn", "mcp:\n  allow_deploy: true\n")
    return tmp_path


@pytest.fixture()
def box(connections, tmp_path):
    created: list[RecordingAdapter] = []

    def factory(cfg):
        adapter = RecordingAdapter()
        adapter.connect(cfg)
        created.append(adapter)
        return adapter

    from db_project_manager.application.mcp_service import ConnectionManager

    manager = ConnectionManager(connections, adapter_factory=factory)
    cfg = CFG(paths=PathsConfig(default_output_dir=str(tmp_path / "out"), logs_dir=str(tmp_path / "logs")))
    return MCPToolBox(manager, cfg)


# --- introspection ---


def test_list_schemas_uses_adapter(box):
    with box.manager.connection("ro_conn") as conn:
        original = conn.adapter.list_schemas

    def fake_list_schemas():
        return ["public", "app"]

    conn.adapter.list_schemas = fake_list_schemas  # type: ignore[method-assign]
    assert box.list_schemas("ro_conn") == ["public", "app"]
    conn.adapter.list_schemas = original  # type: ignore[method-assign]


def test_list_objects_and_details_use_structure(box):
    structure = {
        "schemas": [
            {"name": "public", "tables": [{"name": "t", "comment": None, "columns": [{"name": "id"}]}]}
        ]
    }
    with box.manager.connection("ro_conn") as conn:
        conn.adapter.get_database_structure = lambda: structure  # type: ignore[method-assign]
    assert box.list_objects("ro_conn", "public", "table") == [{"name": "t", "comment": None}]
    details = box.get_object_details("ro_conn", "public", "t", "table")
    assert details["columns"] == [{"name": "id"}]


def test_query_passes_through_service(box):
    result = box.query("ro_conn", "SELECT 1")
    assert result.row_count == 1


def test_query_policy_reaches_tool(box):
    with pytest.raises(MCPPermissionError, match="read-only"):
        box.query("ro_conn", "DROP TABLE t")


# --- deploy_plan ---


class FakePlanResult:
    operations = [1, 2, 3]
    safe_ops = [1]
    needs_pre_ops = [2, 3]
    violations: list[Any] = []


class FakeDeployApplyService:
    instances: list["FakeDeployApplyService"] = []

    def __init__(self, service_schema: str) -> None:
        self.service_schema = service_schema
        self.calls: list[dict[str, Any]] = []
        FakeDeployApplyService.instances.append(self)

    def plan(self, directory, conn_cfg, run_dir, *, include_drops, progress):
        self.calls.append({"op": "plan", "include_drops": include_drops})
        progress("сравнение", 1, 2)
        return FakePlanResult()

    def apply(self, directory, conn_cfg, run_dir, *, include_drops, rehearsal, keep_rehearsal_db, progress):
        self.calls.append({"op": "apply", "include_drops": include_drops, "rehearsal": rehearsal})

        class Result:
            applied = 3
            planned = 3
            applied_version = "2026.09.27.01"
            rehearsal_db = "dbpm_rehearsal_x"
            output_dir = run_dir

        return Result()


@pytest.fixture()
def fake_apply_service(monkeypatch):
    FakeDeployApplyService.instances = []
    monkeypatch.setattr(tools_module, "DeployApplyService", FakeDeployApplyService)
    return FakeDeployApplyService


def test_deploy_plan_read_only_no_gate_needed(box, fake_apply_service):
    result = box.deploy_plan("ro_conn", codebase_dir=".")
    assert result["status"] == "ok"
    assert result["operations"] == 3
    assert result["needs_pre"] == 2
    assert result["progress_tail"] == ["[1/2] сравнение"]
    service = fake_apply_service.instances[0]
    assert service.service_schema == "__deploy"
    assert service.calls[0] == {"op": "plan", "include_drops": False}


def test_deploy_apply_blocked_without_allow_deploy(box, fake_apply_service):
    with pytest.raises(MCPPermissionError, match="allow_deploy"):
        box.deploy_apply("ro_conn")
    assert fake_apply_service.instances == []


def test_deploy_apply_passes_rehearsal_options(box, fake_apply_service):
    result = box.deploy_apply("deploy_conn", include_drops=True, rehearsal=False)
    assert result["status"] == "ok"
    assert result["applied_version"] == "2026.09.27.01"
    service = fake_apply_service.instances[0]
    assert service.calls[0] == {"op": "apply", "include_drops": True, "rehearsal": False}


# --- deploy_analyze ---


class FakeVerdict:
    clean = True
    touched: list[Any] = []
    violations: list[Any] = []
    ignored_build_false = 0


class FakeSafetyService:
    def __init__(self, service_schema: str) -> None:
        pass

    def analyze(self, directory, conn_cfg, run_dir, progress=None):
        return FakeVerdict()


def test_deploy_analyze_clean(box, monkeypatch):
    monkeypatch.setattr(tools_module, "SafetyGateService", FakeSafetyService)
    result = box.deploy_analyze("ro_conn")
    assert result["status"] == "clean"
    assert result["touched_tables"] == 0


# --- deploy_reset ---


class FakeResetPlan:
    schemas = ["public", "junk"]
    in_codebase = {"public"}
    object_counts = {"public": 5, "junk": 2}
    extensions: list[dict[str, Any]] = []

    def is_content_drop(self, schema: str) -> bool:
        return schema == "public"


class FakeResetResult:
    dry_run = False
    schemas_wiped = ["public"]
    schemas_dropped = ["junk"]
    extensions_dropped: list[str] = []
    journal_truncated = True


class FakeSchemaResetService:
    instances: list["FakeSchemaResetService"] = []

    def __init__(self, service_schema: str) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        FakeSchemaResetService.instances.append(self)

    def collect(self, directory, conn_cfg):
        self.calls.append(("collect", {"directory": str(directory)}))
        return FakeResetPlan()

    def execute(self, plan, run_dir, *, dry_run, progress=None):
        self.calls.append(("execute", {"dry_run": dry_run}))
        FakeResetResult.dry_run = dry_run
        return FakeResetResult()


@pytest.fixture()
def fake_reset_service(monkeypatch):
    FakeSchemaResetService.instances = []
    monkeypatch.setattr(tools_module, "SchemaResetService", FakeSchemaResetService)
    return FakeSchemaResetService


def test_deploy_reset_blocked_without_allow_deploy(box, fake_reset_service):
    with pytest.raises(MCPPermissionError, match="allow_deploy"):
        box.deploy_reset("ro_conn", confirm_database="db")


def test_deploy_reset_requires_exact_confirmation(box, fake_reset_service):
    with pytest.raises(MCPPermissionError, match="confirm_database='db'"):
        box.deploy_reset("deploy_conn", confirm_database="wrong")
    assert fake_reset_service.instances == []


def test_deploy_reset_dry_run_skips_confirmation(box, fake_reset_service):
    result = box.deploy_reset("deploy_conn", dry_run=True)
    assert result["status"] == "ok"
    assert result["dry_run"] is True
    assert {p["schema"] for p in result["plan"]} == {"public", "junk"}


def test_deploy_reset_confirmed_executes(box, fake_reset_service):
    result = box.deploy_reset("deploy_conn", confirm_database="db")
    assert result["schemas_dropped"] == 1
    service = fake_reset_service.instances[0]
    assert service.calls[1] == ("execute", {"dry_run": False})


# --- FastMCP registration (skipped when the [mcp] extra is absent) ---


def test_server_registers_all_tools(box):
    pytest.importorskip("mcp")
    import asyncio

    from db_project_manager.presentation.mcp.server import create_server

    server = create_server(box.manager, box.cfg)
    listed = asyncio.run(server.list_tools())
    names = {t.name for t in listed}
    expected = {
        "list_connections",
        "list_schemas",
        "list_objects",
        "get_object_details",
        "query",
        "explain",
        "get_top_queries",
        "run_script",
        "deploy_plan",
        "deploy_analyze",
        "deploy_apply",
        "deploy_reset",
    }
    assert names == expected
    annotations = {t.name: t.annotations for t in listed}
    assert annotations["query"].readOnlyHint is True
    assert annotations["run_script"].destructiveHint is False
    assert annotations["deploy_reset"].destructiveHint is True

"""End-to-end test: build a graph from a codebase reverse-engineered from a
real PostgreSQL container.

Round-trip: deploy codebase_sample to a temp DB via the PG adapter, then
reverse-engineer it back to SQL files, then build the graph from those files
and verify it has the expected objects.
"""

from __future__ import annotations

import pytest

from db_project_manager.application.deploy_service import DeployValidateService
from db_project_manager.application.graph_service import BuildGraphService
from db_project_manager.application.reverse_engineer import build_default_service
from db_project_manager.infrastructure.database.registry import get_adapter


pytestmark = pytest.mark.integration


def test_reverse_engineer_then_build_graph(pg_conn_cfg, codebase_sample_dir, tmp_path):
    # 1. Deploy codebase_sample to a temp DB.
    deploy = DeployValidateService()
    deploy_result = deploy.run(pg_conn_cfg, codebase_sample_dir, keep_db=True)
    assert deploy_result.success, deploy_result.errors

    # 2. Reverse-engineer the deployed DB back to SQL files.
    target_cfg = pg_conn_cfg.model_copy(update={"database": deploy_result.db_name})
    re_service = build_default_service()
    out_dir = tmp_path / "re"
    re_service.run(target_cfg, out_dir)

    # The reverse-engineered codebase root is <out>/<database>.
    re_root = out_dir / deploy_result.db_name
    assert re_root.is_dir()
    # Must contain at least the bookings schema with tables.
    assert (re_root / "bookings" / "tables").is_dir()

    # 3. Build the graph from the reverse-engineered files.
    graph_service = BuildGraphService()
    graph = graph_service.build(re_root)
    table_keys = {
        v.object_name for v in graph.vertices.values() if v.object_type == "table"
    }
    # The four tables of codebase_sample minus the matview (which is type
    # materialized_view, not table).
    assert {"aircrafts", "airports", "flights", "tickets"}.issubset(table_keys)

    # Cleanup.
    try:
        adapter = get_adapter(pg_conn_cfg)
        adapter.connect(pg_conn_cfg)
        adapter.drop_database(deploy_result.db_name)
    finally:
        adapter.disconnect()

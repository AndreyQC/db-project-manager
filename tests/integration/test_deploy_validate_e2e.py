"""End-to-end test: validation deploy on a real PostgreSQL container.

Verifies the full Scenario A from the Phase 2 vision:
  - CREATEDB check passes
  - temp DB is created with server-UTC-timestamp name
  - objects deploy in topological order
  - temp DB is dropped on cleanup (default) / kept with --keep-db
  - build:false object (routes) is skipped
  - objects actually exist in the deployed DB (information_schema check)
"""

from __future__ import annotations

import pytest

from db_project_manager.application.deploy_service import (
    DeployPermissionError,
    DeployValidateService,
    sanitize_prefix,
)
from db_project_manager.infrastructure.database.registry import get_adapter


pytestmark = pytest.mark.integration


def _count_tables_in_db(conn_cfg, schema_name: str) -> int:
    """Connect to a DB and count user tables in the given schema."""
    adapter = get_adapter(conn_cfg)
    try:
        adapter.connect(conn_cfg)
        from sqlalchemy import text
        # access the underlying connection to run a count
        rows = adapter._connection.execute(
            text(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema = :s AND table_type = 'BASE TABLE'"
            ),
            {"s": schema_name},
        ).fetchall()
        return int(rows[0][0]) if rows else 0
    finally:
        adapter.disconnect()


def test_deploy_validate_success_drops_temp_db(pg_conn_cfg, codebase_sample_dir):
    svc = DeployValidateService()
    result = svc.run(pg_conn_cfg, codebase_sample_dir)

    assert result.success, f"deploy failed: {[str(e) for e in result.errors]}"
    assert result.objects_done == result.objects_total
    assert result.objects_total > 0
    # Name carries the prefix derived from the codebase dir + UTC timestamp.
    prefix = sanitize_prefix(codebase_sample_dir.name)
    assert result.db_name.startswith(f"{prefix}_")
    assert result.dropped_db is True  # cleanup happened


def test_deploy_validate_keep_db_leaves_db_behind(pg_conn_cfg, codebase_sample_dir):
    svc = DeployValidateService()
    result = svc.run(pg_conn_cfg, codebase_sample_dir, keep_db=True)

    assert result.success
    assert result.dropped_db is False
    # The temp DB still exists and contains the bookings tables (4 tables;
    # routes is a matview, not a table, and tickets/flights/aircrafts/airports
    # are 4 user tables).
    target_cfg = pg_conn_cfg.model_copy(update={"database": result.db_name})
    try:
        assert _count_tables_in_db(target_cfg, "bookings") == 4
    finally:
        # Cleanup the kept DB manually so the session stays clean.
        adapter = get_adapter(pg_conn_cfg)
        try:
            adapter.connect(pg_conn_cfg)
            adapter.drop_database(result.db_name)
        finally:
            adapter.disconnect()


def test_build_false_object_is_skipped(pg_conn_cfg, codebase_sample_dir):
    """Q8: the build:false matview 'routes' is NOT deployed."""
    svc = DeployValidateService()
    result = svc.run(pg_conn_cfg, codebase_sample_dir, keep_db=True)
    assert result.success

    target_cfg = pg_conn_cfg.model_copy(update={"database": result.db_name})
    try:
        adapter = get_adapter(target_cfg)
        adapter.connect(target_cfg)
        from sqlalchemy import text
        rows = adapter._connection.execute(
            text(
                "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE c.relkind = 'm' AND n.nspname = 'bookings' AND c.relname = 'routes'"
            )
        ).fetchall()
        assert int(rows[0][0]) == 0, "routes matview should not exist (build:false)"
        adapter.disconnect()
    finally:
        adapter = get_adapter(pg_conn_cfg)
        adapter.connect(pg_conn_cfg)
        adapter.drop_database(result.db_name)
        adapter.disconnect()


def test_deploy_without_createdb_raises(pg_conn_cfg, codebase_sample_dir):
    """A non-CREATEDB user gets DeployPermissionError before any DB is created."""
    from db_project_manager.domain.connection import ConnectionConfig
    from sqlalchemy import text

    # Create a restricted role and use it for the deploy attempt.
    admin = get_adapter(pg_conn_cfg)
    admin.connect(pg_conn_cfg)
    try:
        admin._connection.execute(text("DROP ROLE IF EXISTS nocreates;"))
        admin._connection.execute(text("CREATE ROLE nocreates LOGIN PASSWORD 'pw' NOCREATEDB;"))
    finally:
        admin.disconnect()

    restricted_cfg = ConnectionConfig(
        host=pg_conn_cfg.host,
        port=pg_conn_cfg.port,
        database="postgres",
        username="nocreates",
        password="pw",
        type="postgres",
    )
    svc = DeployValidateService()
    with pytest.raises(DeployPermissionError, match="CREATEDB"):
        svc.run(restricted_cfg, codebase_sample_dir)

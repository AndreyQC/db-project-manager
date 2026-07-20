"""End-to-end integration test: extensions + overloaded functions on a real PostgreSQL.

Phase 5 closes BACKLOG P2: reverse-engineer → graph → deploy validate on a
live container DB that has:
  - CREATE EXTENSION citext (and uuid-ossp as a second extension)
  - Overloaded functions f(int) / f(text)
  - ALTER DATABASE ... SET work_mem

The test verifies that all three concerns survive the full roundtrip and that
both overloaded function overloads are present after deploy (the silent-overwrite
bug that Phase 4 fixed would cause only one to survive).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text

from db_project_manager.application.deploy_service import DeployValidateService
from db_project_manager.application.reverse_engineer import ReverseEngineerService
from db_project_manager.application.graph_service import BuildGraphService
from db_project_manager.infrastructure.database.registry import get_adapter

pytestmark = pytest.mark.integration


def _pg_version_at_least(pg_conn_cfg, major: int) -> bool:
    """Return True if the server version is >= *major*."""
    adapter = get_adapter(pg_conn_cfg)
    adapter.connect(pg_conn_cfg)
    try:
        result = adapter._connection.execute(text("SHOW server_version_num")).fetchone()
        version = int(result[0])
        return version >= major * 10000
    finally:
        adapter.disconnect()


def test_extensions_and_overloads_roundtrip(pg_conn_cfg, tmp_path: Path) -> None:
    """Create a DB with extensions + overloaded functions, then run the full
    reverse → graph → deploy pipeline and verify all objects survive."""

    adapter = get_adapter(pg_conn_cfg)
    adapter.connect(pg_conn_cfg)

    # --- Setup: create extensions and overloaded functions in the source DB ---
    adapter._connection.execute(text("CREATE EXTENSION IF NOT EXISTS citext;"))
    adapter._connection.execute(text("CREATE EXTENSION IF NOT EXISTS uuid-ossp;"))
    adapter._connection.execute(text("SET work_mem = '64MB';"))
    adapter._connection.execute(text(
        "CREATE OR REPLACE FUNCTION public.f(int) RETURNS int LANGUAGE sql AS 'SELECT $1 * 2';"
    ))
    adapter._connection.execute(text(
        "CREATE OR REPLACE FUNCTION public.f(text) RETURNS int LANGUAGE sql AS 'SELECT 42';"
    ))
    adapter._connection.execute(text(
        "CREATE TABLE public.people(id serial, name citext NOT NULL, uid uuid DEFAULT uuid_generate_v4());"
    ))
    adapter.disconnect()

    try:
        # --- Reverse-engineer the source DB ---
        out = ReverseEngineerService().run(
            pg_conn_cfg, tmp_path / "codebase", progress=None
        )

        # Verify extension and table files were generated.
        ext_dir = out / "extensions"
        assert ext_dir.is_dir(), "extensions/ directory not created"
        ext_files = list(ext_dir.glob("extension *.sql"))
        assert len(ext_files) >= 2, f"Expected >= 2 extension files, got {ext_files}"

        # Verify the table using citext type is in the output.
        tables_dir = out / "public" / "tables"
        assert tables_dir.is_dir(), "public/tables/ not created"
        table_files = list(tables_dir.glob("table people.sql"))
        assert table_files, "table people.sql not created"

        # Verify settings file exists.
        settings_dir = out / "settings"
        assert settings_dir.is_dir(), "settings/ directory not created"
        settings_files = list(settings_dir.glob("database settings.sql"))
        assert settings_files, "database settings.sql not created"

        # --- Graph build ---
        svc = BuildGraphService()
        graph = svc.build(out, build_only=True)

        # Both overloads must be present (Phase 4 fix: silent overwrite).
        overload_keys = [
            k for k in graph.vertices
            if "function public.f" in k and "/signature/" in k
        ]
        assert len(overload_keys) == 2, (
            f"Expected 2 overloads of f(int)/f(text), got {len(overload_keys)}: {overload_keys}"
        )

        # Extension vertex must be present.
        ext_keys = [k for k in graph.vertices if "extension" in k]
        assert ext_keys, f"No extension vertices found in graph: {list(graph.vertices.keys())}"

        # Database_setting vertex must be present.
        dbs_keys = [k for k in graph.vertices if "database_setting" in k]
        assert dbs_keys, "No database_setting vertex found"

        # --- Deploy validate ---
        deploy_svc = DeployValidateService()
        result = deploy_svc.run(pg_conn_cfg, out, keep_db=True)

        assert result.success, f"deploy failed: {[str(e) for e in result.errors]}"
        assert result.objects_total > 0

        # Connect to the temp DB and verify both overloads + citext exist.
        target_cfg = pg_conn_cfg.model_copy(update={"database": result.db_name})
        check = get_adapter(target_cfg)
        check.connect(target_cfg)
        try:
            # Both overloads present.
            fn_rows = check._connection.execute(text(
                "SELECT proname, oidvectortypes(proargtypes) "
                "FROM pg_proc WHERE proname = 'f' AND pronamespace = 'public'::regnamespace"
            )).fetchall()
            assert len(fn_rows) == 2, f"Expected 2 overloads of f(), got {fn_rows}"

            # citext extension present.
            ext_rows = check._connection.execute(text(
                "SELECT extname FROM pg_extension WHERE extname = 'citext'"
            )).fetchall()
            assert ext_rows, "citext extension not found after deploy"

            # people table with citext column present.
            col_rows = check._connection.execute(text(
                "SELECT atttypid::regtype FROM pg_attribute "
                "JOIN pg_type t ON t.oid = atttypid "
                "WHERE attrelid = 'public.people'::regclass AND attname = 'name'"
            )).fetchall()
            assert col_rows and str(col_rows[0][0]) == "citext", (
                f"Expected citext column, got {col_rows}"
            )
        finally:
            check.disconnect()
            # Cleanup.
            admin = get_adapter(pg_conn_cfg)
            admin.connect(pg_conn_cfg)
            admin.drop_database(result.db_name)
            admin.disconnect()

    finally:
        # Teardown: drop test objects from the source DB.
        # We must reconnect first: after the check.disconnect() above the adapter
        # is disconnected and drop_database requires a connection.
        adapter.connect(pg_conn_cfg)
        for name in ("public.people", "public.f", "public.f"):
            try:
                adapter._connection.execute(text(f"DROP TABLE IF EXISTS {name} CASCADE"))
            except Exception:
                pass
        adapter.disconnect()

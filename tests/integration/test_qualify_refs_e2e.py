"""End-to-end integration test for the qualify-refs post-processor.

Verifies the full Phase 6 flow on a real PostgreSQL container:
  1. Create a DB with two functions where one calls the other with a BARE name.
  2. Reverse-engineer → bare ref lands in the generated function body.
  3. QualifyRefsService qualifies it (auto via reverse-engineer hook).
  4. deploy validate succeeds (because the ref is now schema-qualified).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text

from db_project_manager.application.deploy_service import DeployValidateService
from db_project_manager.application.reverse_engineer import build_default_service
from db_project_manager.infrastructure.database.registry import get_adapter

pytestmark = pytest.mark.integration


def test_bare_function_call_qualified_and_deploys(pg_conn_cfg, tmp_path: Path) -> None:
    """A function calling another with a bare name survives reverse→deploy
    because QualifyRefsService qualifies it with the schema."""
    adapter = get_adapter(pg_conn_cfg)
    adapter.connect(pg_conn_cfg)

    # Create two functions: sp_helper (callee) and sp_caller (caller, BARE ref).
    adapter._connection.execute(text(
        "CREATE OR REPLACE FUNCTION public.sp_helper(x int) RETURNS int "
        "LANGUAGE sql AS 'SELECT x * 2';"
    ))
    adapter._connection.execute(text(
        # NOTE: bare call to sp_helper — no schema prefix. This is what the user
        # hits in real DBs and what Phase 6 fixes automatically.
        "CREATE OR REPLACE FUNCTION public.sp_caller(x int) RETURNS int "
        "LANGUAGE sql AS 'SELECT sp_helper(x)';"
    ))
    adapter.disconnect()

    try:
        # Reverse-engineer — the default service auto-runs QualifyRefsService.
        svc = build_default_service()
        out = svc.run(pg_conn_cfg, tmp_path / "codebase", progress=None)

        # The caller file must now contain the qualified call.
        caller_file = out / "public" / "functions" / "function sp_caller.sql"
        assert caller_file.is_file(), f"function sp_caller.sql not generated at {caller_file}"
        body = caller_file.read_text(encoding="utf-8")
        # Bare call must be gone, qualified call must be present.
        assert "public.sp_helper(" in body, f"bare call not qualified:\n{body}"
        # The autodoc header should record the change.
        assert "qualify_report" in body

        # The qualify report file must exist.
        report = out / "_qualify_report.md"
        assert report.is_file()
        report_text = report.read_text(encoding="utf-8")
        assert "sp_helper" in report_text

        # deploy validate must succeed — the qualified call resolves regardless
        # of search_path on the temp DB.
        deploy_svc = DeployValidateService()
        result = deploy_svc.run(pg_conn_cfg, out, keep_db=True)
        assert result.success, (
            f"deploy validate failed: {[str(e) for e in result.errors]}"
        )

        # Cleanup the kept temp DB.
        admin = get_adapter(pg_conn_cfg)
        admin.connect(pg_conn_cfg)
        admin.drop_database(result.db_name)
        admin.disconnect()

    finally:
        # Teardown source objects.
        adapter.connect(pg_conn_cfg)
        for name in ("public.sp_caller(int)", "public.sp_helper(int)"):
            try:
                adapter._connection.execute(text(f"DROP FUNCTION IF EXISTS {name}"))
            except Exception:
                pass
        adapter.disconnect()

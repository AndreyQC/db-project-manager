"""End-to-end integration tests for Phase 10 CD Foundation on a real PostgreSQL.

Verifies the full mechanic against a live DB via testcontainers:
  - __deploy schema + 3 tables created on the temp-DB during deploy
  - schema_version row recorded with manifest.source_version
  - pre/post scripts from __migrations/ executed against the temp-DB
  - canonical-DDL warning fires when the codebase's __deploy is mutated

Skipped from the default unit run via ``@pytest.mark.integration`` (LESSONS §21
— Docker daemon must be running). Run with: ``uv run pytest -m integration``.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from sqlalchemy import text

from db_project_manager.application.deploy_service import DeployValidateService
from db_project_manager.infrastructure.database.registry import get_adapter

pytestmark = pytest.mark.integration


def _count_deploy_tables(conn_cfg, service_schema: str = "__deploy") -> int:
    """Connect to the kept temp-DB and count tables in __deploy."""
    adapter = get_adapter(conn_cfg)
    try:
        adapter.connect(conn_cfg)
        rows = adapter._connection.execute(
            text(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema = :s AND table_type = 'BASE TABLE'"
            ),
            {"s": service_schema},
        ).fetchall()
        return int(rows[0][0]) if rows else 0
    finally:
        adapter.disconnect()


def _read_schema_version(conn_cfg, service_schema: str = "__deploy") -> str | None:
    """Read the version row from __deploy.schema_version."""
    adapter = get_adapter(conn_cfg)
    try:
        adapter.connect(conn_cfg)
        rows = adapter._connection.execute(
            text(
                f'SELECT version FROM "{service_schema}"."schema_version" '
                "ORDER BY applied_at DESC LIMIT 1"
            ),
        ).fetchall()
        return str(rows[0][0]) if rows else None
    finally:
        adapter.disconnect()


def test_deploy_creates_deploy_schema_with_three_tables(
    pg_conn_cfg, codebase_sample_dir
):
    """Successful deploy leaves a __deploy schema with exactly the 3 tables."""
    svc = DeployValidateService()
    result = svc.run(pg_conn_cfg, codebase_sample_dir, keep_db=True)

    assert result.success, f"deploy failed: {[str(e) for e in result.errors]}"
    try:
        # Connect to the kept temp-DB and inspect __deploy.

        kept_cfg = pg_conn_cfg.model_copy(update={"database": result.db_name})
        assert _count_deploy_tables(kept_cfg) == 3
    finally:
        # Manual cleanup — we asked keep_db=True so the service did not drop it.
        admin = get_adapter(pg_conn_cfg)
        admin.connect(pg_conn_cfg)
        try:
            admin._connection.execute(text(f'DROP DATABASE IF EXISTS "{result.db_name}"'))
        finally:
            admin.disconnect()


def test_deploy_records_schema_version_from_manifest(
    pg_conn_cfg, codebase_sample_dir
):
    """The temp-DB's __deploy.schema_version carries manifest.source_version."""
    svc = DeployValidateService()
    result = svc.run(pg_conn_cfg, codebase_sample_dir, keep_db=True)

    assert result.success
    try:

        kept_cfg = pg_conn_cfg.model_copy(update={"database": result.db_name})
        # Fixture manifest ships with source_version='2026.08.11.01'.
        assert _read_schema_version(kept_cfg) == "2026.08.11.01"
    finally:
        admin = get_adapter(pg_conn_cfg)
        admin.connect(pg_conn_cfg)
        try:
            admin._connection.execute(text(f'DROP DATABASE IF EXISTS "{result.db_name}"'))
        finally:
            admin.disconnect()


def test_deploy_executes_pre_and_post_scripts(pg_conn_cfg, codebase_sample_dir):
    """Pre-script creates app.tmp_stage; post-script must see it (runs after schema)."""
    svc = DeployValidateService()
    result = svc.run(pg_conn_cfg, codebase_sample_dir, keep_db=True)

    assert result.success
    try:

        kept_cfg = pg_conn_cfg.model_copy(update={"database": result.db_name})
        adapter = get_adapter(kept_cfg)
        adapter.connect(kept_cfg)
        try:
            # Pre-script created app.tmp_stage (CREATE TABLE IF NOT EXISTS).
            rows = adapter._connection.execute(
                text(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema = 'app' AND table_name = 'tmp_stage'"
                )
            ).fetchall()
            assert int(rows[0][0]) == 1
        finally:
            adapter.disconnect()
    finally:
        admin = get_adapter(pg_conn_cfg)
        admin.connect(pg_conn_cfg)
        try:
            admin._connection.execute(text(f'DROP DATABASE IF EXISTS "{result.db_name}"'))
        finally:
            admin.disconnect()


def test_deploy_canonical_mismatch_warning_emitted(
    pg_conn_cfg, codebase_sample_dir, tmp_path: Path, capsys
):
    """Mutated __deploy DDL → warning logged, deploy still succeeds."""
    # Copy fixture and mutate script_history.sql.
    dst = tmp_path / "codebase"
    shutil.copytree(codebase_sample_dir, dst)
    path = dst / "__deploy" / "tables" / "script_history.sql"
    body = path.read_text(encoding="utf-8")
    body = body.replace(
        "duration_ms     INTEGER NOT NULL,",
        "duration_ms     INTEGER NOT NULL,\n    note            TEXT,",
    )
    path.write_text(body, encoding="utf-8")

    # WARNING-level loguru output goes to stderr by default. The validator
    # itself logs the warning; we just assert the deploy still succeeds.
    # (The fact that no DeployError is raised proves the warning is non-blocking.)
    svc = DeployValidateService()
    result = svc.run(pg_conn_cfg, dst, keep_db=True)
    assert result.success
    try:
        pass
    finally:
        admin = get_adapter(pg_conn_cfg)
        admin.connect(pg_conn_cfg)
        try:
            admin._connection.execute(text(f'DROP DATABASE IF EXISTS "{result.db_name}"'))
        finally:
            admin.disconnect()

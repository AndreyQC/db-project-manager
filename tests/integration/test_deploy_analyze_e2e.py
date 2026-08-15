"""End-to-end integration tests for Phase 11 Safety Gate (deploy analyze).

Against a real PostgreSQL (testcontainers): the target DB holds actual data,
the codebase comes from reverse-engineering that same DB and is then mutated
per scenario. Verifies CD-6..CD-10 semantics and the read-only contract.

Run with: uv run pytest -m integration (Docker must be running).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text

from db_project_manager.application.reverse_engineer import ReverseEngineerService
from db_project_manager.application.safety_gate_service import (
    SafetyGateError,
    SafetyGateService,
)
from db_project_manager.infrastructure.database.registry import get_adapter

pytestmark = pytest.mark.integration

SCHEMA = "sg_app"

_PRE_SCRIPT = """/*====
[<[autodoc-yaml]]
object:
  object_type: pre_script
  object_name: migrate_orders
project:
  build: true
  covers:
    - sg_app.orders
[[autodoc-yaml]>]
====*/

-- Idempotent data migration placeholder (the gate never executes it).
SELECT 1;
"""


def _exec(conn_cfg, statements: list[str]) -> None:
    adapter = get_adapter(conn_cfg)
    adapter.connect(conn_cfg)
    try:
        for stmt in statements:
            adapter._connection.execute(text(stmt))
    finally:
        adapter.disconnect()


def _init_target(conn_cfg) -> None:
    """(Re)create the target schema: orders with 100 rows, empty_t empty."""
    _exec(
        conn_cfg,
        [
            f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE',
            'DROP SCHEMA IF EXISTS "__deploy" CASCADE',
            f'CREATE SCHEMA "{SCHEMA}"',
            f'CREATE TABLE "{SCHEMA}"."orders" (id integer PRIMARY KEY, note text)',
            f'CREATE TABLE "{SCHEMA}"."empty_t" (id integer)',
            f'INSERT INTO "{SCHEMA}"."orders" SELECT g, \'n\' || g FROM generate_series(1, 100) g',
            # ANALYZE so reltuples/last_analyze are deterministic (SG-4: fresh stats).
            f'ANALYZE "{SCHEMA}"."orders"',
            f'ANALYZE "{SCHEMA}"."empty_t"',
        ],
    )


def _codebase(conn_cfg, tmp_path: Path) -> Path:
    """Reverse-engineer the live target into a codebase dir; return its root."""
    out_root = tmp_path / "re"
    ReverseEngineerService().run(conn_cfg, out_root)
    return out_root / conn_cfg.database


def _orders_file(codebase: Path) -> Path:
    return codebase / SCHEMA / "tables" / "table orders.sql"


def _orders_state(conn_cfg) -> tuple[int, list[str]]:
    """(row count, column list) of sg_app.orders in the live DB (two queries —
    a single FROM over both relations would be a cross join)."""
    adapter = get_adapter(conn_cfg)
    adapter.connect(conn_cfg)
    try:
        count = int(
            adapter._connection.execute(text(f'SELECT count(*) FROM "{SCHEMA}"."orders"'))
            .scalar()
        )
        rows = adapter._connection.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = :s AND table_name = 'orders' "
                "ORDER BY ordinal_position"
            ),
            {"s": SCHEMA},
        ).fetchall()
        return count, [r[0] for r in rows]
    finally:
        adapter.disconnect()


def _empty_file(codebase: Path) -> Path:
    return codebase / SCHEMA / "tables" / "table empty_t.sql"


def _modify(operand: Path, old: str, new: str) -> None:
    body = operand.read_text(encoding="utf-8")
    assert old in body, f"fixture drift: {old!r} not in {operand}"
    operand.write_text(body.replace(old, new), encoding="utf-8")


def test_changed_data_table_uncovered_fails(pg_conn_cfg, tmp_path):
    """CD-9/CD-10: a data-bearing CHANGED table without a covering pre-script
    is a violation; the pipeline signal is clean=False; the DB is untouched."""
    _init_target(pg_conn_cfg)
    codebase = _codebase(pg_conn_cfg, tmp_path)
    _modify(_orders_file(codebase), '"note"', '"note2"')  # column rename in DDL

    out = tmp_path / "report"
    verdict = SafetyGateService().analyze(codebase, pg_conn_cfg, out)

    assert verdict.clean is False
    violations = verdict.violations
    assert len(violations) == 1
    v = violations[0]
    assert (v.object_schema, v.name) == (SCHEMA, "orders")
    assert v.presence.value == "has_data"
    assert v.estimated_rows == 100
    assert v.covered_by == []
    # Report artifacts written next to diff_report.json (SG-2).
    assert (out / "safety_gate_report.md").is_file()
    assert (out / "safety_gate_report.json").is_file()
    assert (out / "diff_report.json").is_file()
    # Read-only contract: the target still has its data and original shape.
    assert _orders_state(pg_conn_cfg) == (100, ["id", "note"])


def test_covered_pre_script_passes(pg_conn_cfg, tmp_path):
    """CD-8: a pre-script declaring project.covers turns the violation into a pass."""
    _init_target(pg_conn_cfg)
    codebase = _codebase(pg_conn_cfg, tmp_path)
    _modify(_orders_file(codebase), '"note"', '"note2"')
    pre = codebase / "__migrations" / "pre"
    pre.mkdir(parents=True)
    (pre / "2026-08-14_001_migrate_orders.sql").write_text(_PRE_SCRIPT, encoding="utf-8")

    verdict = SafetyGateService().analyze(codebase, pg_conn_cfg, tmp_path / "report")

    assert verdict.clean is True
    assert verdict.touched[0].covered_by == ["2026-08-14_001_migrate_orders.sql"]


def test_empty_table_changed_passes(pg_conn_cfg, tmp_path):
    """CD-10: changes to EMPTY tables are allowed without a pre-script."""
    _init_target(pg_conn_cfg)
    codebase = _codebase(pg_conn_cfg, tmp_path)
    _modify(_empty_file(codebase), '"id"', '"id2"')

    verdict = SafetyGateService().analyze(codebase, pg_conn_cfg, tmp_path / "report")

    assert verdict.clean is True
    assert len(verdict.touched) == 1
    t = verdict.touched[0]
    assert (t.object_schema, t.name) == (SCHEMA, "empty_t")
    assert t.presence.value == "empty"


def test_added_objects_pass(pg_conn_cfg, tmp_path):
    """CD-10: new objects (and the seeded __deploy tables) are ADDED — ignored."""
    _init_target(pg_conn_cfg)
    codebase = _codebase(pg_conn_cfg, tmp_path)
    # A brand-new table file copied from orders (valid autodoc + DDL).
    brand_new = codebase / SCHEMA / "tables" / "table brand_new.sql"
    brand_new.write_text(
        _orders_file(codebase).read_text(encoding="utf-8").replace("orders", "brand_new"),
        encoding="utf-8",
    )

    verdict = SafetyGateService().analyze(codebase, pg_conn_cfg, tmp_path / "report")

    assert verdict.clean is True
    assert verdict.touched == []


def test_target_newer_version_hard_error(pg_conn_cfg, tmp_path):
    """SG-6: a target __deploy.schema_version newer than the codebase → hard error.

    The version is written AFTER reverse-engineering on purpose: RE syncs the
    manifest's source_version from the target's __deploy (Phase 10 S6), so a
    pre-existing version would end up equal, not newer.
    """
    _init_target(pg_conn_cfg)
    codebase = _codebase(pg_conn_cfg, tmp_path)
    _exec(
        pg_conn_cfg,
        [
            'CREATE SCHEMA IF NOT EXISTS "__deploy"',
            'CREATE TABLE IF NOT EXISTS "__deploy"."schema_version" '
            "(id SERIAL PRIMARY KEY, version TEXT NOT NULL, "
            "applied_at TIMESTAMPTZ NOT NULL DEFAULT now(), source TEXT NOT NULL)",
            'INSERT INTO "__deploy"."schema_version" (version, source) '
            "VALUES ('2099.01.01.01', 'deploy')",
        ],
    )

    with pytest.raises(SafetyGateError, match="новее"):
        SafetyGateService().analyze(codebase, pg_conn_cfg, tmp_path / "report")

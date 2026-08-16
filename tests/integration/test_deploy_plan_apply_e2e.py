"""End-to-end integration tests for Phase 12 (deploy plan / deploy apply).

Against a real PostgreSQL (testcontainers): the target holds actual data, the
codebase comes from reverse-engineering that same target and is then mutated per
scenario. Verifies CD-ALT-1..4 / CD-11..15 semantics: safe-alter on a data table
(gate violation downgraded via ALT-3 classification), drop-column blocked,
covers-resolving pre-script, CD-11 residual check, rehearsal isolation, retry
after a mid-delta failure, unextractable-columns fail-safe, type synonyms, and
seed scripts running only in the rehearsal.

Run with: uv run pytest -m integration (Docker must be running).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from sqlalchemy import text

from db_project_manager.application.deploy_apply_service import (
    DeployApplyError,
    DeployApplyRejected,
    DeployApplyService,
)
from db_project_manager.application.reverse_engineer import ReverseEngineerService
from db_project_manager.domain.delta import OperationClass
from db_project_manager.infrastructure.config.codebase_manifest import MANIFEST_FILENAME
from db_project_manager.infrastructure.database.registry import get_adapter
from db_project_manager.infrastructure.sql.autodoc import extract_header, render_header

pytestmark = pytest.mark.integration

SCHEMA = "pa_app"
VERSION = "2026.08.16.01"

_PRE_WITH_COVERS = """/*====
[<[autodoc-yaml]]
object:
  object_type: pre_script
  object_name: migrate_orders
project:
  build: true
  covers:
    - pa_app.orders
[[autodoc-yaml]>]
====*/

{body}
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
    """(Re)create the target: orders with 100 rows, empty_t empty, __deploy ready.

    __deploy is created from the canonical templates (what a previous real
    deploy would have left) so that RE renders it into the codebase and
    record_schema_version works on the target.
    """
    from db_project_manager.infrastructure.deploy.canonical_ddl import (
        canonical_deploy_ddl,
    )

    statements = [
        f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE',
        'DROP SCHEMA IF EXISTS "__deploy" CASCADE',
        f'CREATE SCHEMA "{SCHEMA}"',
        f'CREATE TABLE "{SCHEMA}"."orders" (id integer PRIMARY KEY, note text)',
        f'CREATE TABLE "{SCHEMA}"."empty_t" (id integer)',
        f'INSERT INTO "{SCHEMA}"."orders" SELECT g, \'n\' || g FROM generate_series(1, 100) g',
        f'ANALYZE "{SCHEMA}"."orders"',
        f'ANALYZE "{SCHEMA}"."empty_t"',
        'CREATE SCHEMA "__deploy"',
    ]
    statements.extend(canonical_deploy_ddl().values())
    _exec(conn_cfg, statements)


def _codebase(conn_cfg, tmp_path: Path) -> Path:
    out_root = tmp_path / "re"
    ReverseEngineerService().run(conn_cfg, out_root)
    return out_root / conn_cfg.database


def _bump(codebase: Path, version: str = VERSION) -> None:
    manifest = codebase / MANIFEST_FILENAME
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["source_version"] = version
    manifest.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _orders_file(codebase: Path) -> Path:
    return codebase / SCHEMA / "tables" / "table orders.sql"


def _add_note2(codebase: Path) -> None:
    """Code-first edit: add a nullable column (safe per ALT-3, even with data).

    RE quotes identifiers and renders NULL explicitly: `"note" text NULL`.
    """
    path = _orders_file(codebase)
    body = path.read_text(encoding="utf-8")
    assert '"note" text NULL' in body, "fixture drift: no note column"
    path.write_text(
        body.replace(
            '"note" text NULL', '"note" text NULL,\n    "note2" text NULL', 1
        ),
        encoding="utf-8",
    )


def _drop_note(codebase: Path) -> None:
    """Code-first edit: remove the note column line entirely."""
    path = _orders_file(codebase)
    body = path.read_text(encoding="utf-8")
    lines = [
        ln for ln in body.splitlines()
        if not re.match(r'^\s*"note" text NULL,?\s*$', ln)
    ]
    assert len(lines) < len(body.splitlines()), "fixture drift: no note column removed"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _orders_state(conn_cfg) -> tuple[int, list[str]]:
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


def _recorded_version(conn_cfg) -> list[tuple[str, str]]:
    adapter = get_adapter(conn_cfg)
    adapter.connect(conn_cfg)
    try:
        rows = adapter._connection.execute(
            text('SELECT version, source FROM "__deploy"."schema_version"')
        ).fetchall()
        return [(r[0], r[1]) for r in rows]
    finally:
        adapter.disconnect()


def _write_pre(codebase: Path, body: str) -> None:
    pre = codebase / "__migrations" / "pre"
    pre.mkdir(parents=True, exist_ok=True)
    (pre / "2026-08-16_001_migrate_orders.sql").write_text(
        _PRE_WITH_COVERS.format(body=body), encoding="utf-8"
    )


def _orders_op(plan):
    return next(
        (op for op in plan.operations if op.object_name == "orders"), None
    )


# ------------------------------------------------------------------ scenarios


def test_add_nullable_column_safe_applied(pg_conn_cfg, tmp_path):
    """(1) Safe alter on a data table: gate violation downgraded (ALT-3), the
    column appears, the version is recorded with source='apply' (CD-15)."""
    _init_target(pg_conn_cfg)
    codebase = _codebase(pg_conn_cfg, tmp_path)
    _bump(codebase)
    _add_note2(codebase)

    service = DeployApplyService()
    out = tmp_path / "out"
    plan = service.plan(codebase, pg_conn_cfg, out)
    op = _orders_op(plan)
    assert op is not None and op.action == "alter"
    assert op.classification is OperationClass.SAFE

    result = service.apply(codebase, pg_conn_cfg, out)
    assert _orders_state(pg_conn_cfg) == (100, ["id", "note", "note2"])
    assert (VERSION, "apply") in _recorded_version(pg_conn_cfg)
    assert (out / "plan.json").is_file()
    assert (out / "rehearsal" / "plan.json").is_file()
    assert result.rehearsal_db


def test_drop_column_with_data_needs_pre_blocked(pg_conn_cfg, tmp_path):
    """(2) Drop column on a data table without a pre-script: plan shows
    non-safe, apply is rejected before mutating the target."""
    _init_target(pg_conn_cfg)
    codebase = _codebase(pg_conn_cfg, tmp_path)
    _bump(codebase)
    _drop_note(codebase)

    service = DeployApplyService()
    plan = service.plan(codebase, pg_conn_cfg, tmp_path / "out")
    op = _orders_op(plan)
    assert op is not None
    assert op.classification in (OperationClass.NEEDS_PRE, OperationClass.BLOCKED)
    # plan.md documents the blocked operation (review artifact, CD-13)
    assert (tmp_path / "out" / "plan.md").is_file()

    with pytest.raises(DeployApplyRejected):
        service.apply(codebase, pg_conn_cfg, tmp_path / "out", rehearsal=False)
    assert _orders_state(pg_conn_cfg) == (100, ["id", "note"])
    assert _recorded_version(pg_conn_cfg) == []


def test_covers_pre_script_resolves(pg_conn_cfg, tmp_path):
    """(3) A covering pre-script that performs the drop itself: gate passes,
    CD-11 sees a clean residual delta, apply completes."""
    _init_target(pg_conn_cfg)
    codebase = _codebase(pg_conn_cfg, tmp_path)
    _bump(codebase)
    _drop_note(codebase)
    _write_pre(codebase, f'ALTER TABLE "{SCHEMA}"."orders" DROP COLUMN IF EXISTS note;')

    result = DeployApplyService().apply(
        codebase, pg_conn_cfg, tmp_path / "out", rehearsal=False
    )
    assert _orders_state(pg_conn_cfg) == (100, ["id"])   # data preserved
    assert (VERSION, "apply") in _recorded_version(pg_conn_cfg)
    assert result.applied_version == VERSION


def test_rehearsal_failure_target_untouched(pg_conn_cfg, tmp_path):
    """(4) A broken pre-script fails on the rehearsal analog: hard error, the
    live target keeps its original shape (ALT-5)."""
    _init_target(pg_conn_cfg)
    codebase = _codebase(pg_conn_cfg, tmp_path)
    _bump(codebase)
    _add_note2(codebase)
    _write_pre(codebase, "THIS IS NOT SQL ;;;")

    with pytest.raises(DeployApplyError, match="Репетиция"):
        DeployApplyService().apply(codebase, pg_conn_cfg, tmp_path / "out")
    assert _orders_state(pg_conn_cfg) == (100, ["id", "note"])
    assert _recorded_version(pg_conn_cfg) == []


def test_retry_after_midway_error_completes(pg_conn_cfg, tmp_path):
    """(5) Stop-on-error mid-delta (no rehearsal), then fix and re-apply: the
    delta is re-computed, the run completes, the version is recorded once."""
    _init_target(pg_conn_cfg)
    codebase = _codebase(pg_conn_cfg, tmp_path)
    _bump(codebase)
    _add_note2(codebase)

    broken = codebase / SCHEMA / "tables" / "table broken_t.sql"
    meta = extract_header(_orders_file(codebase).read_text(encoding="utf-8"))
    meta["object"]["object_name"] = "broken_t"
    meta["object"]["object_key"] = meta["object"]["object_key"].replace(
        "name/orders", "name/broken_t"
    )
    broken.write_text(
        render_header(meta) + f'CREATE TABLE "{SCHEMA}"."broken_t" (id integer ;',
        encoding="utf-8",
    )

    service = DeployApplyService()
    with pytest.raises(DeployApplyError):
        service.apply(codebase, pg_conn_cfg, tmp_path / "out", rehearsal=False)
    assert _recorded_version(pg_conn_cfg) == []   # no version after a failed run

    broken.write_text(
        render_header(meta) + f'CREATE TABLE "{SCHEMA}"."broken_t" (id integer);',
        encoding="utf-8",
    )
    result = service.apply(codebase, pg_conn_cfg, tmp_path / "out", rehearsal=False)
    assert result.applied >= 1
    assert _orders_state(pg_conn_cfg)[1] == ["id", "note", "note2"]
    assert (VERSION, "apply") in _recorded_version(pg_conn_cfg)
    # broken_t now exists
    adapter = get_adapter(pg_conn_cfg)
    adapter.connect(pg_conn_cfg)
    try:
        exists = adapter._connection.execute(text(
            "SELECT to_regclass(:r) IS NOT NULL", ), {"r": f"{SCHEMA}.broken_t"},
        ).scalar()
    finally:
        adapter.disconnect()
    assert exists


def test_unextractable_columns_failsafe(pg_conn_cfg, tmp_path):
    """(6) A CTAS body yields columns=None → structural diff unavailable →
    fail-safe non-safe classification (ALT-1b/ALT-2)."""
    _init_target(pg_conn_cfg)
    codebase = _codebase(pg_conn_cfg, tmp_path)
    _bump(codebase)
    path = _orders_file(codebase)
    meta = extract_header(path.read_text(encoding="utf-8"))
    path.write_text(
        render_header(meta)
        + f'CREATE TABLE "{SCHEMA}"."orders" AS SELECT 1 AS id, \'n\' AS note;\n',
        encoding="utf-8",
    )

    plan = DeployApplyService().plan(codebase, pg_conn_cfg, tmp_path / "out")
    op = _orders_op(plan)
    assert op is not None
    assert op.classification in (OperationClass.NEEDS_PRE, OperationClass.BLOCKED)
    assert "недоступна" in op.reason


def test_type_synonyms_no_false_change(pg_conn_cfg, tmp_path):
    """(7) int4 vs integer: one canonical form → UNCHANGED, no plan operation."""
    _init_target(pg_conn_cfg)
    codebase = _codebase(pg_conn_cfg, tmp_path)
    _bump(codebase)
    path = _orders_file(codebase)
    body = path.read_text(encoding="utf-8")
    assert '"id" int4' in body, "fixture drift: expected RE to write udt int4"
    path.write_text(body.replace('"id" int4', '"id" integer'), encoding="utf-8")

    plan = DeployApplyService().plan(codebase, pg_conn_cfg, tmp_path / "out")
    op = _orders_op(plan)
    assert op is None or op.action == "skip"


def test_seed_runs_only_in_rehearsal(pg_conn_cfg, tmp_path):
    """(8) Seed scripts populate the rehearsal analog only; the target's row
    count is unaffected by a successful apply (ALT-8)."""
    _init_target(pg_conn_cfg)
    codebase = _codebase(pg_conn_cfg, tmp_path)
    _bump(codebase)
    _add_note2(codebase)
    seed = codebase / "__migrations" / "seed"
    seed.mkdir(parents=True)
    (seed / "2026-08-16_001_seed.sql").write_text(
        f'INSERT INTO "{SCHEMA}"."orders" (id, note) VALUES (9999, \'seed\');\n',
        encoding="utf-8",
    )

    result = DeployApplyService().apply(
        codebase, pg_conn_cfg, tmp_path / "out", keep_rehearsal_db=True
    )
    assert _orders_state(pg_conn_cfg) == (100, ["id", "note", "note2"])  # untouched rows

    # The rehearsal analog reproduces the target SCHEMA (not data): the seed
    # row is the only one there — proving seeds ran in the rehearsal only.
    rehearsal_cfg = pg_conn_cfg.model_copy(update={"database": result.rehearsal_db})
    adapter = get_adapter(rehearsal_cfg)
    adapter.connect(rehearsal_cfg)
    try:
        rehearsal_rows = int(
            adapter._connection.execute(
                text(f'SELECT count(*) FROM "{SCHEMA}"."orders"')
            ).scalar()
        )
    finally:
        adapter.disconnect()
    assert rehearsal_rows == 1  # seed row, and only the seed row

    admin = get_adapter(pg_conn_cfg)
    admin.connect(pg_conn_cfg)
    try:
        admin.drop_database(result.rehearsal_db)
    finally:
        admin.disconnect()

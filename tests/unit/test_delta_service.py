"""Tests for db_project_manager.application.delta_service (Phase 12, S5).

Covers plan ordering (deploy_order toposort, REMOVED last, build=false excluded),
artifact content per action (alter/rerender/drop/commented-drop/comment-only skip),
and the plan.json round-trip + plan.md rendering contract.
"""

from __future__ import annotations

from pathlib import Path

from db_project_manager.application.delta_service import DeltaService
from db_project_manager.domain.delta import (
    ColumnChangeKind,
    ColumnDiff,
    ColumnSnapshot,
    DeltaPlan,
    OperationClass,
)
from db_project_manager.domain.diff import (
    DiffEntry,
    DiffStatus,
    ObjectSnapshot,
    SnapshotSourceKind,
    StateSnapshot,
)
from db_project_manager.domain.safety import StatsConfidence, TablePresenceStats
from db_project_manager.infrastructure.deploy.plan_report import (
    JSON_OUTPUT_NAME,
    MD_OUTPUT_NAME,
)
from db_project_manager.infrastructure.sql.autodoc import build_metadata, render_header

CATALOG = "testdb"


def _key(object_type: str, name: str, schema: str = "app") -> str:
    meta = build_metadata(
        object_catalog=CATALOG, object_schema=schema,
        object_type=object_type, object_name=name,
    )
    return meta["object"]["object_key"]


def _write_object(codebase: Path, object_type: str, name: str, body: str,
                  schema: str = "app", build: bool = True) -> str:
    meta = build_metadata(
        object_catalog=CATALOG, object_schema=schema,
        object_type=object_type, object_name=name,
    )
    if not build:
        meta["project"]["build"] = False
    header = render_header(meta).replace("build: true", "build: false") if not build \
        else render_header(meta)
    type_dir = codebase / schema / f"{object_type}s"
    type_dir.mkdir(parents=True, exist_ok=True)
    (type_dir / f"{object_type} {name}.sql").write_text(
        header + body, encoding="utf-8", newline="\n"
    )
    return meta["object"]["object_key"]


def _snap(object_type: str, name: str, sql_hash: str, schema: str = "app",
          columns: list[ColumnSnapshot] | None = None) -> ObjectSnapshot:
    return ObjectSnapshot(
        object_key=_key(object_type, name, schema),
        object_schema=schema, object_name=name, object_type=object_type,
        sql_normalized="x", sql_hash=sql_hash,
        columns=columns if columns is not None else ([] if object_type == "table" else None),
    )


def _entry(status: DiffStatus, src: ObjectSnapshot | None, tgt: ObjectSnapshot | None,
           column_diffs: list[ColumnDiff] | None = None,
           columns_unavailable: bool = False) -> DiffEntry:
    assert src is not None or tgt is not None
    return DiffEntry(
        object_key=(src or tgt).object_key, status=status,
        source_snapshot=src, target_snapshot=tgt,
        column_diffs=column_diffs or [], columns_unavailable=columns_unavailable,
    )


def _report(entries: list[DiffEntry]) -> object:
    empty_state = StateSnapshot(
        source_kind=SnapshotSourceKind.DIR, source_ref="src", db_type="postgres",
        generated_at="2026-08-16T00:00:00+00:00", objects={}, edges=[],
    )
    return type("R", (), {
        "entries": entries,
        "source": empty_state, "target": empty_state,
        "generated_at": "2026-08-16T00:00:00+00:00", "summary": {},
    })()


def _stats(schema: str, name: str, rows: int | None, confidence=StatsConfidence.FRESH):
    return TablePresenceStats(
        object_schema=schema, name=name, estimated_rows=rows, confidence=confidence
    )


ORDERS_BODY = "CREATE TABLE app.orders (id int NOT NULL, note text);\n"
NEW_T_BODY = "CREATE TABLE app.new_t (id int NOT NULL);\n"
T_OLD_BODY = "CREATE TABLE app.t_old (id int NOT NULL);\n"
V1_BODY = "CREATE OR REPLACE VIEW app.v1 AS SELECT 1;\n"
F1_BODY = "CREATE OR REPLACE FUNCTION app.f1() RETURNS int LANGUAGE sql AS $$ SELECT 1 $$;\n"


def _make_codebase(tmp_path: Path) -> Path:
    """Codebase WITHOUT t_old: REMOVED objects exist only on the target side."""
    root = tmp_path / "codebase"
    _write_object(root, "table", "orders", ORDERS_BODY)
    _write_object(root, "table", "new_t", NEW_T_BODY)
    _write_object(root, "view", "v1", V1_BODY)
    _write_object(root, "function", "f1", F1_BODY)
    return root


def _added_note_diff() -> list[ColumnDiff]:
    return [ColumnDiff(
        column="note", kind=ColumnChangeKind.ADDED,
        source_column=ColumnSnapshot(name="note", type="text", nullable=True),
    )]


# ------------------------------------------------------------------- ordering


def test_plan_orders_by_deploy_order_and_removed_last(tmp_path: Path) -> None:
    root = _make_codebase(tmp_path)
    entries = [
        _entry(DiffStatus.UNCHANGED, _snap("function", "f1", "h"), _snap("function", "f1", "h")),
        _entry(DiffStatus.CHANGED, _snap("view", "v1", "h1"), _snap("view", "v1", "h2")),
        _entry(DiffStatus.REMOVED, None, _snap("table", "t_old", "h")),
    ]
    plan = DeltaService().build_plan(
        root, _report(entries), stats={}, coverage={}, db_type="postgres",
        source_version="2026.08.16.01", target_version=None,
    )
    names = [op.object_name for op in plan.operations]
    # type priority: table < view < function; REMOVED — last regardless
    assert names == ["v1", "f1", "t_old"]


def test_build_false_objects_excluded_from_plan(tmp_path: Path) -> None:
    root = tmp_path / "codebase"
    _write_object(root, "table", "orders", ORDERS_BODY)
    _write_object(root, "table", "skipped_t", "CREATE TABLE app.skipped_t (id int);\n",
                  build=False)
    entries = [
        _entry(DiffStatus.CHANGED, _snap("table", "skipped_t", "h1"),
               _snap("table", "skipped_t", "h2")),
    ]
    plan = DeltaService().build_plan(
        root, _report(entries), stats={}, coverage={}, db_type="postgres",
    )
    assert plan.operations == []


# ------------------------------------------------------------------ artifacts


def _build_alter_plan(tmp_path: Path, include_drops: bool = False) -> tuple[DeltaPlan, Path, Path]:
    root = _make_codebase(tmp_path)
    entries = [
        _entry(
            DiffStatus.CHANGED,
            _snap("table", "orders", "h1", columns=[
                ColumnSnapshot(name="id", type="int", nullable=False),
                ColumnSnapshot(name="note", type="text", nullable=True),
            ]),
            _snap("table", "orders", "h2", columns=[
                ColumnSnapshot(name="id", type="int", nullable=False),
            ]),
            column_diffs=_added_note_diff(),
        ),
    ]
    stats = {("app", "orders"): _stats("app", "orders", 100)}
    plan = DeltaService().build_plan(
        root, _report(entries), stats=stats, coverage={}, db_type="postgres",
        include_drops=include_drops,
    )
    out = tmp_path / "out"
    return plan, root, out


def test_safe_alter_artifact_written(tmp_path: Path) -> None:
    plan, root, out = _build_alter_plan(tmp_path)
    written = DeltaService().write_artifacts(plan, root, out)
    alter_path = out / "delta" / "001_table_app_orders.sql"
    assert alter_path in written
    content = alter_path.read_text(encoding="utf-8")
    assert 'ALTER TABLE "app"."orders" ADD COLUMN "note" text;' in content
    # plan.json written last and references the artifact
    assert plan.operations[0].script_file == "delta/001_table_app_orders.sql"
    assert (out / JSON_OUTPUT_NAME).exists() and (out / MD_OUTPUT_NAME).exists()


def test_empty_table_rerender_has_drop_prefix(tmp_path: Path) -> None:
    root = _make_codebase(tmp_path)
    entries = [
        _entry(DiffStatus.CHANGED, _snap("table", "orders", "h1"),
               _snap("table", "orders", "h2")),
    ]
    stats = {("app", "orders"): _stats("app", "orders", 0)}   # 0 + FRESH → EMPTY
    plan = DeltaService().build_plan(
        root, _report(entries), stats=stats, coverage={}, db_type="postgres",
    )
    out = tmp_path / "out"
    DeltaService().write_artifacts(plan, root, out)
    content = (out / "delta" / "001_table_app_orders.sql").read_text(encoding="utf-8")
    assert content.startswith('DROP TABLE IF EXISTS "app"."orders";')
    assert "CREATE TABLE app.orders" in content


def test_removed_blocked_drop_is_commented(tmp_path: Path) -> None:
    root = _make_codebase(tmp_path)
    entries = [
        _entry(DiffStatus.REMOVED, None, _snap("table", "t_old", "h")),
    ]
    stats = {("app", "t_old"): _stats("app", "t_old", 50)}
    plan = DeltaService().build_plan(
        root, _report(entries), stats=stats, coverage={}, db_type="postgres",
    )
    assert plan.operations[0].classification is OperationClass.BLOCKED
    out = tmp_path / "out"
    DeltaService().write_artifacts(plan, root, out)
    content = (out / "delta" / "001_table_app_t_old.sql").read_text(encoding="utf-8")
    assert content.startswith("-- BLOCKED:")
    assert '-- DROP TABLE IF EXISTS "app"."t_old";' in content


def test_removed_with_flag_and_empty_writes_executable_drop(tmp_path: Path) -> None:
    root = _make_codebase(tmp_path)
    entries = [
        _entry(DiffStatus.REMOVED, None, _snap("table", "t_old", "h")),
    ]
    stats = {("app", "t_old"): _stats("app", "t_old", 0)}
    plan = DeltaService().build_plan(
        root, _report(entries), stats=stats, coverage={}, db_type="postgres",
        include_drops=True,
    )
    assert plan.operations[0].classification is OperationClass.SAFE
    out = tmp_path / "out"
    DeltaService().write_artifacts(plan, root, out)
    content = (out / "delta" / "001_table_app_t_old.sql").read_text(encoding="utf-8")
    assert content.strip() == 'DROP TABLE IF EXISTS "app"."t_old";'


def test_comment_only_body_downgraded_to_skip(tmp_path: Path) -> None:
    root = tmp_path / "codebase"
    _write_object(root, "table", "stub", "-- только комментарии, исполняемого SQL нет\n")
    entries = [
        _entry(DiffStatus.ADDED, _snap("table", "stub", "h"), None),
    ]
    plan = DeltaService().build_plan(
        root, _report(entries), stats={}, coverage={}, db_type="postgres",
    )
    out = tmp_path / "out"
    written = DeltaService().write_artifacts(plan, root, out)
    assert plan.operations[0].action == "skip"
    assert not any(p.name.endswith("table_app_stub.sql") for p in written)
    # plan.json reflects the downgrade (round-trip, LESSONS §28)
    restored = DeltaPlan.model_validate_json((out / JSON_OUTPUT_NAME).read_text(encoding="utf-8"))
    assert restored.operations[0].action == "skip"


# --------------------------------------------------------------- report files


def test_plan_json_roundtrip_and_md_content(tmp_path: Path) -> None:
    plan, root, out = _build_alter_plan(tmp_path)
    DeltaService().write_artifacts(plan, root, out)
    restored = DeltaPlan.model_validate_json((out / JSON_OUTPUT_NAME).read_text(encoding="utf-8"))
    assert restored == plan
    md = (out / MD_OUTPUT_NAME).read_text(encoding="utf-8")
    assert "app.orders" in md
    assert "safe" in md
    assert plan.operations[0].reason in md


def test_plan_md_shows_needs_pre_recommendation(tmp_path: Path) -> None:
    root = _make_codebase(tmp_path)
    entries = [
        _entry(
            DiffStatus.CHANGED,
            _snap("table", "orders", "h1", columns=[
                ColumnSnapshot(name="id", type="int", nullable=False),
            ]),
            _snap("table", "orders", "h2", columns=[
                ColumnSnapshot(name="id", type="int", nullable=False),
                ColumnSnapshot(name="secret", type="text", nullable=True),
            ]),
            column_diffs=[ColumnDiff(column="secret", kind=ColumnChangeKind.DROPPED,
                                     target_column=ColumnSnapshot(name="secret", type="text",
                                                                  nullable=True))],
        ),
    ]
    entries.append(_entry(DiffStatus.CHANGED,
                          _snap("table", "orders2", "h1"), _snap("table", "orders2", "h2"),
                          columns_unavailable=True))
    _write_object(root, "table", "orders2", "CREATE TABLE app.orders2 (id int);\n")
    stats = {("app", "orders"): _stats("app", "orders", 10),
             ("app", "orders2"): _stats("app", "orders2", 10)}
    plan = DeltaService().build_plan(
        root, _report(entries), stats=stats, coverage={}, db_type="postgres",
    )
    out = tmp_path / "out"
    DeltaService().write_artifacts(plan, root, out)
    md = (out / MD_OUTPUT_NAME).read_text(encoding="utf-8")
    assert "Требуют pre-скриптов" in md
    assert 'project.covers: ["app.orders"]' in md
    assert 'project.covers: ["app.orders2"]' in md

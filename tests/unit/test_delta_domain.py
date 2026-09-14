"""Tests for db_project_manager.domain.delta (Phase 12, step S1).

Pure-domain coverage: pydantic round-trips (LESSONS §28), enum-as-string serialization,
backwards compatibility of the extended diff models (old JSON without the Phase 12
fields must keep parsing), and the DeltaPlan classification properties. No DB, no
filesystem, no sqlglot.
"""

from __future__ import annotations

from db_project_manager.domain.delta import (
    ColumnChangeKind,
    ColumnDiff,
    ColumnSnapshot,
    DeltaPlan,
    OperationClass,
    PlannedOperation,
)
from db_project_manager.domain.diff import DiffEntry, DiffStatus, ObjectSnapshot


def _column(name: str = "id", type_: str = "int4", nullable: bool = False,
            default: str | None = None) -> ColumnSnapshot:
    return ColumnSnapshot(name=name, type=type_, nullable=nullable, default=default)


def _operation(object_key: str, classification: OperationClass,
               action: str = "alter") -> PlannedOperation:
    return PlannedOperation(
        object_key=object_key,
        object_type="table",
        object_schema="app",
        object_name=object_key.rsplit("/", 1)[-1],
        action=action,  # type: ignore[arg-type]
        classification=classification,
        reason="test",
    )


# ------------------------------------------------------------- ColumnSnapshot


def test_column_snapshot_roundtrip() -> None:
    col = _column("note", "varchar(255)", nullable=True, default="'n/a'")
    restored = ColumnSnapshot.model_validate_json(col.model_dump_json())
    assert restored == col
    assert restored.name == "note"
    assert restored.type == "varchar(255)"
    assert restored.nullable is True
    assert restored.default == "'n/a'"


def test_column_snapshot_default_is_none() -> None:
    assert _column().default is None
    assert _column().nullable is False


# ---------------------------------------------------------------- ColumnDiff


def test_column_diff_kinds_serialize_as_strings() -> None:
    diff = ColumnDiff(
        column="c",
        kind=ColumnChangeKind.TYPE_CHANGED,
        source_column=_column("c", "int4"),
        target_column=_column("c", "bigint"),
    )
    data = diff.model_dump(mode="json")
    assert data["kind"] == "type_changed"
    assert data["source_column"]["type"] == "int4"
    assert data["target_column"]["type"] == "bigint"
    assert ColumnDiff.model_validate(data) == diff


def test_column_diff_added_has_only_source() -> None:
    diff = ColumnDiff(
        column="new_col",
        kind=ColumnChangeKind.ADDED,
        source_column=_column("new_col", "text", nullable=True),
    )
    assert diff.target_column is None
    restored = ColumnDiff.model_validate_json(diff.model_dump_json())
    assert restored.kind is ColumnChangeKind.ADDED
    assert restored.source_column is not None
    assert restored.source_column.nullable is True


# ----------------------------------------------------------- PlannedOperation


def test_planned_operation_roundtrip() -> None:
    op = PlannedOperation(
        object_key="pg_database/demo/schema/app/type/table/name/orders",
        object_type="table",
        object_schema="app",
        object_name="orders",
        action="alter",
        column_diffs=[
            ColumnDiff(
                column="note",
                kind=ColumnChangeKind.ADDED,
                source_column=_column("note", "text", nullable=True),
            ),
        ],
        classification=OperationClass.SAFE,
        reason="ADD COLUMN nullable без DEFAULT (PG11+ metadata-only)",
        script_file="delta/003_table_app_orders.sql",
    )
    restored = PlannedOperation.model_validate_json(op.model_dump_json())
    assert restored == op
    assert restored.action == "alter"
    assert restored.classification is OperationClass.SAFE
    assert restored.column_diffs[0].kind is ColumnChangeKind.ADDED
    assert restored.script_file.endswith("orders.sql")


def test_planned_operation_defaults() -> None:
    op = _operation("k", OperationClass.BLOCKED, action="skip")
    assert op.column_diffs == []
    assert op.script_file == ""
    assert op.object_schema == "app"


def test_planned_operation_rejects_unknown_action() -> None:
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        PlannedOperation(
            object_key="k", object_type="table", object_name="t",
            action="explode",  # type: ignore[arg-type]
            classification=OperationClass.SAFE, reason="bad action",
        )


# ----------------------------------------------------------------- DeltaPlan


def _plan(ops: list[PlannedOperation]) -> DeltaPlan:
    return DeltaPlan(
        db_type="postgres",
        source_version="2026.08.16.01",
        target_version="2026.08.15.03",
        operations=ops,
    )


def test_delta_plan_properties_partition_operations() -> None:
    safe = _operation("a", OperationClass.SAFE)
    needs_pre = _operation("b", OperationClass.NEEDS_PRE)
    blocked = _operation("c", OperationClass.BLOCKED)
    plan = _plan([safe, needs_pre, blocked])

    assert plan.safe_ops == [safe]
    assert plan.needs_pre_ops == [needs_pre]
    assert plan.violations == [blocked]
    # partition is exhaustive and stable (order preserved within each class)
    assert len(plan.safe_ops) + len(plan.needs_pre_ops) + len(plan.violations) == len(plan.operations)


def test_delta_plan_roundtrip() -> None:
    plan = _plan([
        _operation("a", OperationClass.SAFE),
        _operation("b", OperationClass.NEEDS_PRE),
    ])
    plan.include_drops = True
    restored = DeltaPlan.model_validate_json(plan.model_dump_json())
    assert restored == plan
    assert restored.include_drops is True
    assert restored.db_type == "postgres"


def test_delta_plan_empty_operations() -> None:
    plan = DeltaPlan(db_type="postgres")
    assert plan.operations == []
    assert plan.violations == []
    assert plan.include_drops is False


# ----------------------------- diff-models extension (backwards compatibility)


def test_object_snapshot_old_json_parses_columns_none() -> None:
    """A pre-Phase-12 source.json entry has no `columns` → None (not extracted)."""
    payload = {
        "object_key": "pg_database/demo/schema/app/type/table/name/orders",
        "object_type": "table",
        "object_name": "orders",
        "sql_normalized": "create table app.orders (id int)",
        "sql_hash": "abcd1234",
    }
    snap = ObjectSnapshot.model_validate(payload)
    assert snap.columns is None


def test_object_snapshot_columns_roundtrip() -> None:
    snap = ObjectSnapshot(
        object_key="k", object_type="table", object_name="t",
        sql_normalized="x", sql_hash="h",
        columns=[_column("id", "int4"), _column("note", "text", nullable=True)],
    )
    restored = ObjectSnapshot.model_validate_json(snap.model_dump_json())
    assert restored.columns is not None
    assert [c.name for c in restored.columns] == ["id", "note"]
    assert restored.columns[1].nullable is True


def test_object_snapshot_empty_columns_list_is_preserved() -> None:
    """[] means 'extracted, no columns' — must not collapse to None."""
    snap = ObjectSnapshot(
        object_key="k", object_type="table", object_name="t",
        sql_normalized="x", sql_hash="h", columns=[],
    )
    restored = ObjectSnapshot.model_validate_json(snap.model_dump_json())
    assert restored.columns == []


def test_diff_entry_old_json_parses_new_fields_default() -> None:
    payload = {
        "object_key": "k",
        "status": "changed",
        "source_snapshot": {
            "object_key": "k", "object_type": "table", "object_name": "t",
            "sql_normalized": "x", "sql_hash": "h1",
        },
        "target_snapshot": {
            "object_key": "k", "object_type": "table", "object_name": "t",
            "sql_normalized": "x", "sql_hash": "h2",
        },
    }
    entry = DiffEntry.model_validate(payload)
    assert entry.status is DiffStatus.CHANGED
    assert entry.column_diffs == []
    assert entry.columns_unavailable is False


def test_diff_entry_new_fields_roundtrip() -> None:
    entry = DiffEntry(
        object_key="k",
        status=DiffStatus.CHANGED,
        column_diffs=[ColumnDiff(column="c", kind=ColumnChangeKind.DROPPED)],
        columns_unavailable=False,
    )
    restored = DiffEntry.model_validate_json(entry.model_dump_json())
    assert restored.column_diffs[0].kind is ColumnChangeKind.DROPPED
    assert restored.columns_unavailable is False


def test_diff_entry_columns_unavailable_flag() -> None:
    entry = DiffEntry(object_key="k", status=DiffStatus.CHANGED, columns_unavailable=True)
    assert entry.columns_unavailable is True

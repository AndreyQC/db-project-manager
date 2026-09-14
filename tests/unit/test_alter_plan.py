"""Tests for db_project_manager.infrastructure.deploy.alter_plan (Phase 12, S4).

Covers the full ALT-3 classification matrix (change × presence × coverage ×
include_drops), volatile-default detection, and the render contract: fully-quoted
identifiers (LESSONS §34/§35), whitelist rejections (§19), no DDL for unsafe classes.
"""

from __future__ import annotations

import pytest

from db_project_manager.domain.delta import (
    ColumnChangeKind,
    ColumnDiff,
    ColumnSnapshot,
    OperationClass,
)
from db_project_manager.domain.diff import (
    DiffEntry,
    DiffStatus,
    ObjectSnapshot,
)
from db_project_manager.domain.safety import DataPresence
from db_project_manager.infrastructure.deploy.alter_plan import (
    classify,
    is_safe_identifier,
    is_volatile_default,
    quote_identifier,
    render_alter,
)

DATA = DataPresence.HAS_DATA
UNKNOWN = DataPresence.UNKNOWN
EMPTY = DataPresence.EMPTY


def _col(name: str = "c", type_: str = "int", nullable: bool = True,
         default: str | None = None) -> ColumnSnapshot:
    return ColumnSnapshot(name=name, type=type_, nullable=nullable, default=default)


def _diff(kind: ColumnChangeKind, column: str = "c", *, src: ColumnSnapshot | None = None,
          tgt: ColumnSnapshot | None = None, **kwargs) -> ColumnDiff:
    return ColumnDiff(
        column=column, kind=kind,
        source_column=src if src is not None else _col(column, **kwargs),
        target_column=tgt if tgt is not None else _col(column, **kwargs),
    )


def _entry(status: DiffStatus, object_type: str = "table",
           column_diffs: list[ColumnDiff] | None = None,
           columns_unavailable: bool = False,
           schema: str = "app", name: str = "orders") -> DiffEntry:
    src = ObjectSnapshot(
        object_key=f"pg_database/d/schema/{schema}/type/{object_type}/name/{name}",
        object_schema=schema, object_name=name, object_type=object_type,
        sql_normalized="x", sql_hash="h1",
        columns=[] if object_type == "table" else None,
    )
    tgt = ObjectSnapshot(
        object_key=src.object_key, object_schema=schema, object_name=name,
        object_type=object_type, sql_normalized="x", sql_hash="h2",
        columns=[] if object_type == "table" else None,
    )
    return DiffEntry(
        object_key=src.object_key, status=status,
        source_snapshot=src if status != DiffStatus.REMOVED else None,
        target_snapshot=tgt if status != DiffStatus.ADDED else None,
        column_diffs=column_diffs or [],
        columns_unavailable=columns_unavailable,
    )


def _classify(diff_kind_pairs=None, *, status=DiffStatus.CHANGED, presence=DATA,
              covered=False, include_drops=False, object_type="table",
              columns_unavailable=False):
    diffs = (
        [d if isinstance(d, ColumnDiff) else _diff(*d) for d in diff_kind_pairs]
        if diff_kind_pairs is not None
        else []
    )
    entry = _entry(status, object_type=object_type, column_diffs=diffs,
                  columns_unavailable=columns_unavailable)
    return classify(entry, presence, covered, include_drops=include_drops)


# ------------------------------------------------------ status-level matrix


def test_unchanged_skips() -> None:
    op = _classify(status=DiffStatus.UNCHANGED)
    assert op.action == "skip" and op.classification is OperationClass.SAFE


def test_added_object_creates_safe() -> None:
    op = _classify(status=DiffStatus.ADDED)
    assert op.action == "create" and op.classification is OperationClass.SAFE


def test_removed_without_flag_blocks() -> None:
    op = _classify(status=DiffStatus.REMOVED)
    assert op.action == "drop"   # drop-candidate, but execution is gated by BLOCKED
    assert op.classification is OperationClass.BLOCKED
    assert "--include-drops" in op.reason


def test_removed_with_flag_drops_non_table() -> None:
    op = _classify(status=DiffStatus.REMOVED, object_type="view", include_drops=True)
    assert op.action == "drop" and op.classification is OperationClass.SAFE


def test_removed_with_flag_empty_table_drops() -> None:
    op = _classify(status=DiffStatus.REMOVED, presence=EMPTY, include_drops=True)
    assert op.action == "drop" and op.classification is OperationClass.SAFE


def test_removed_with_flag_data_table_still_blocks() -> None:
    op = _classify(status=DiffStatus.REMOVED, presence=DATA, include_drops=True)
    assert op.classification is OperationClass.BLOCKED
    op_unknown = _classify(status=DiffStatus.REMOVED, presence=UNKNOWN, include_drops=True)
    assert op_unknown.classification is OperationClass.BLOCKED


def test_changed_non_table_rerenders_safe() -> None:
    op = _classify(object_type="view")
    assert op.action == "rerender" and op.classification is OperationClass.SAFE


def test_changed_empty_table_rerenders_safe() -> None:
    op = _classify(presence=EMPTY, diff_kind_pairs=[(ColumnChangeKind.DROPPED,)])
    assert op.action == "rerender" and op.classification is OperationClass.SAFE


# ------------------------------------------------- column matrix (with data)


def test_added_nullable_column_safe_on_data_table() -> None:
    op = _classify(diff_kind_pairs=[(ColumnChangeKind.ADDED,)])
    assert op.action == "alter" and op.classification is OperationClass.SAFE


def test_added_column_with_literal_default_safe() -> None:
    diff = _diff(ColumnChangeKind.ADDED, default="42")
    op = _classify(diff_kind_pairs=[diff])
    assert op.classification is OperationClass.SAFE
    assert "DEFAULT 42" in render_alter(op)


def test_added_column_with_volatile_default_needs_pre() -> None:
    diff = _diff(ColumnChangeKind.ADDED, default="now()")
    op = _classify(diff_kind_pairs=[diff], covered=True)
    assert op.classification is OperationClass.NEEDS_PRE


def test_added_not_null_column_needs_pre() -> None:
    diff = _diff(ColumnChangeKind.ADDED, nullable=False, default="42")
    op = _classify(diff_kind_pairs=[diff], covered=True)
    assert op.classification is OperationClass.NEEDS_PRE


@pytest.mark.parametrize("kind", [
    ColumnChangeKind.DROPPED,
    ColumnChangeKind.TYPE_CHANGED,
])
def test_drop_and_type_change_needs_pre(kind) -> None:
    op = _classify(diff_kind_pairs=[(kind,)], covered=True)
    assert op.classification is OperationClass.NEEDS_PRE


def test_nullability_widening_safe() -> None:
    diff = ColumnDiff(
        column="c", kind=ColumnChangeKind.NULLABILITY_CHANGED,
        source_column=_col("c", nullable=True), target_column=_col("c", nullable=False),
    )
    op = _classify(diff_kind_pairs=[diff])
    assert op.classification is OperationClass.SAFE


def test_nullability_narrowing_needs_pre() -> None:  # covered=True: проверяем правило, а не блокировку
    diff = ColumnDiff(
        column="c", kind=ColumnChangeKind.NULLABILITY_CHANGED,
        source_column=_col("c", nullable=False), target_column=_col("c", nullable=True),
    )
    op = _classify(diff_kind_pairs=[diff], covered=True)
    assert op.classification is OperationClass.NEEDS_PRE


def test_default_addition_safe() -> None:
    diff = ColumnDiff(
        column="c", kind=ColumnChangeKind.DEFAULT_CHANGED,
        source_column=_col("c", default="42"), target_column=_col("c", default=None),
    )
    op = _classify(diff_kind_pairs=[diff])
    assert op.classification is OperationClass.SAFE


def test_default_removal_needs_pre() -> None:  # covered=True: проверяем правило, а не блокировку
    diff = ColumnDiff(
        column="c", kind=ColumnChangeKind.DEFAULT_CHANGED,
        source_column=_col("c", default=None), target_column=_col("c", default="42"),
    )
    op = _classify(diff_kind_pairs=[diff], covered=True)
    assert op.classification is OperationClass.NEEDS_PRE


def test_default_replacement_needs_pre() -> None:  # covered=True: проверяем правило, а не блокировку
    diff = ColumnDiff(
        column="c", kind=ColumnChangeKind.DEFAULT_CHANGED,
        source_column=_col("c", default="1"), target_column=_col("c", default="2"),
    )
    op = _classify(diff_kind_pairs=[diff], covered=True)
    assert op.classification is OperationClass.NEEDS_PRE


def test_safe_plus_unsafe_diff_needs_pre() -> None:
    diffs = [
        _diff(ColumnChangeKind.ADDED, column="new_c"),
        _diff(ColumnChangeKind.DROPPED, column="old_c"),
    ]
    op = _classify(diff_kind_pairs=diffs, covered=True)
    assert op.classification is OperationClass.NEEDS_PRE
    assert "old_c" in op.reason


def test_unrepresented_change_needs_pre() -> None:
    op = _classify(diff_kind_pairs=[], covered=True)
    assert op.classification is OperationClass.NEEDS_PRE
    assert "вне колонок" in op.reason


def test_columns_unavailable_needs_pre() -> None:
    op = _classify(columns_unavailable=True, covered=True)
    assert op.classification is OperationClass.NEEDS_PRE


def test_unknown_presence_behaves_like_has_data() -> None:
    op = _classify(presence=UNKNOWN, diff_kind_pairs=[(ColumnChangeKind.DROPPED,)], covered=True)
    assert op.classification is OperationClass.NEEDS_PRE


# ------------------------------------------------------------ coverage logic


def test_needs_pre_covered_stays_needs_pre() -> None:
    op = _classify(diff_kind_pairs=[(ColumnChangeKind.DROPPED,)], covered=True)
    assert op.classification is OperationClass.NEEDS_PRE
    assert "покрыта pre-скриптом" in op.reason


def test_needs_pre_uncovered_becomes_blocked() -> None:
    op = _classify(diff_kind_pairs=[(ColumnChangeKind.DROPPED,)], covered=False)
    assert op.classification is OperationClass.BLOCKED


# ------------------------------------------------------- volatile defaults


@pytest.mark.parametrize("default", ["42", "'x'", "NULL", "true", "false", "-1", "42::int"])
def test_literal_defaults_not_volatile(default: str) -> None:
    assert is_volatile_default(default) is False


@pytest.mark.parametrize(
    "default",
    [
        "now()", "CURRENT_TIMESTAMP", "uuid_generate_v4()",
        "nextval('app.t_id_seq')", "random()", "THIS IS ((( NOT SQL",
    ],
)
def test_volatile_defaults(default: str) -> None:
    assert is_volatile_default(default) is True


def test_none_default_not_volatile() -> None:
    assert is_volatile_default(None) is False


# ---------------------------------------------------------- render contract


def test_render_fully_qualified_and_quoted() -> None:
    diffs = [_diff(ColumnChangeKind.ADDED, column="note", type_="text")]
    op = _classify(diff_kind_pairs=diffs)
    ddl = render_alter(op)
    assert '"app"."orders"' in ddl
    assert 'ADD COLUMN "note" text;' in ddl


def test_render_widening_and_default_addition() -> None:
    diffs = [
        ColumnDiff(
            column="c", kind=ColumnChangeKind.NULLABILITY_CHANGED,
            source_column=_col("c", nullable=True), target_column=_col("c", nullable=False),
        ),
        ColumnDiff(
            column="d", kind=ColumnChangeKind.DEFAULT_CHANGED,
            source_column=_col("d", default="'n/a'"), target_column=_col("d", default=None),
        ),
    ]
    op = _classify(diff_kind_pairs=diffs)
    ddl = render_alter(op)
    assert 'ALTER COLUMN "c" DROP NOT NULL;' in ddl
    assert 'ALTER COLUMN "d" SET DEFAULT \'n/a\';' in ddl


def test_render_empty_for_needs_pre_and_blocked() -> None:
    op = _classify(diff_kind_pairs=[(ColumnChangeKind.DROPPED,)], covered=True)
    assert render_alter(op) == ""
    op_blocked = _classify(diff_kind_pairs=[(ColumnChangeKind.DROPPED,)])
    assert render_alter(op_blocked) == ""


def test_render_empty_for_non_alter_actions() -> None:
    assert render_alter(_classify(status=DiffStatus.ADDED)) == ""
    assert render_alter(_classify(object_type="view")) == ""
    assert render_alter(_classify(status=DiffStatus.UNCHANGED)) == ""


# ------------------------------------------------------- identifier whitelist


def test_quote_identifier_contracts() -> None:
    assert quote_identifier("orders") == '"orders"'
    assert quote_identifier("_deploy") == '"_deploy"'


@pytest.mark.parametrize("bad", ["has space", 'weird"name', "dash-name", "", "1starts-digit", "semi;colon"])
def test_quote_identifier_rejects_invalid(bad: str) -> None:
    with pytest.raises(ValueError):
        quote_identifier(bad)
    assert is_safe_identifier(bad) is False


def test_unsafe_column_name_downgrades_to_needs_pre() -> None:
    """Column outside the whitelist must not produce DDL — pre-script instead (§19)."""
    diff = _diff(ColumnChangeKind.ADDED, column="weird name")
    op = _classify(diff_kind_pairs=[diff], covered=True)
    assert op.classification is not OperationClass.SAFE

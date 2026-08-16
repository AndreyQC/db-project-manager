"""Tests for column-level diffing (Phase 12, step S3).

Covers ``diff_columns`` (pure function, CD-ALT-1) and the comparator integration
(``column_diffs`` / ``columns_unavailable`` on CHANGED table entries). Direction is
relative to the source (codebase) side throughout.
"""

from __future__ import annotations

from db_project_manager.domain.delta import ColumnChangeKind, ColumnSnapshot
from db_project_manager.domain.diff import (
    DiffStatus,
    ObjectSnapshot,
    SnapshotSourceKind,
    StateSnapshot,
)
from db_project_manager.infrastructure.diff.columns import diff_columns
from db_project_manager.infrastructure.diff.comparator import compare


def _col(name: str, type_: str = "int", nullable: bool = True,
         default: str | None = None) -> ColumnSnapshot:
    return ColumnSnapshot(name=name, type=type_, nullable=nullable, default=default)


def _kinds(diffs) -> dict[str, list[ColumnChangeKind]]:
    out: dict[str, list[ColumnChangeKind]] = {}
    for d in diffs:
        out.setdefault(d.column, []).append(d.kind)
    return out


# ---------------------------------------------------------------- diff_columns


def test_no_changes_returns_empty() -> None:
    src = [_col("a", "int", False), _col("b", "text")]
    tgt = [_col("a", "int", False), _col("b", "text")]
    assert diff_columns(src, tgt) == []


def test_added_and_dropped() -> None:
    src = [_col("a"), _col("new_col", "text")]
    tgt = [_col("a"), _col("old_col", "text")]
    diffs = diff_columns(src, tgt)
    kinds = _kinds(diffs)
    assert kinds == {
        "new_col": [ColumnChangeKind.ADDED],
        "old_col": [ColumnChangeKind.DROPPED],
    }
    added = next(d for d in diffs if d.column == "new_col")
    assert added.source_column is not None and added.target_column is None
    dropped = next(d for d in diffs if d.column == "old_col")
    assert dropped.source_column is None and dropped.target_column is not None


def test_type_changed() -> None:
    diffs = diff_columns([_col("n", "int")], [_col("n", "bigint")])
    assert _kinds(diffs) == {"n": [ColumnChangeKind.TYPE_CHANGED]}


def test_nullability_changed() -> None:
    diffs = diff_columns([_col("n", "int", False)], [_col("n", "int", True)])
    assert _kinds(diffs) == {"n": [ColumnChangeKind.NULLABILITY_CHANGED]}


def test_default_changed() -> None:
    diffs = diff_columns([_col("n", "int", default="42")], [_col("n", "int")])
    assert _kinds(diffs) == {"n": [ColumnChangeKind.DEFAULT_CHANGED]}


def test_multiple_changes_on_one_column() -> None:
    """type + nullable + default changed → three separate records."""
    src = [_col("n", "int", True, None)]
    tgt = [_col("n", "bigint", False, "0")]
    kinds = _kinds(diff_columns(src, tgt))
    assert set(kinds["n"]) == {
        ColumnChangeKind.TYPE_CHANGED,
        ColumnChangeKind.NULLABILITY_CHANGED,
        ColumnChangeKind.DEFAULT_CHANGED,
    }


def test_mixed_changes_sorted_deterministically() -> None:
    src = [_col("z"), _col("a", "int"), _col("mid", "text", default="'x'")]
    tgt = [_col("z", "int"), _col("a", "bigint"), _col("gone"), _col("fresh")]
    diffs = diff_columns(src, tgt)
    keys = [(d.column, d.kind.value) for d in diffs]
    assert keys == sorted(keys)


def test_rename_manifests_as_drop_and_add() -> None:
    """ALT-3: renames are NOT detected — this is the deliberate safe behaviour."""
    src = [_col("full_name", "text")]
    tgt = [_col("name", "text")]
    kinds = _kinds(diff_columns(src, tgt))
    assert kinds == {
        "full_name": [ColumnChangeKind.ADDED],
        "name": [ColumnChangeKind.DROPPED],
    }


def test_default_whitespace_difference_is_not_a_change() -> None:
    src = [_col("n", "int", default="nextval( 'app.t_id_seq' )")]
    tgt = [_col("n", "int", default="nextval('app.t_id_seq')")]
    assert diff_columns(src, tgt) == []


def test_default_whitespace_inside_literal_is_significant() -> None:
    """'a b' must not collapse to 'ab' — literals carry meaning."""
    src = [_col("n", "text", default="'a b'")]
    tgt = [_col("n", "text", default="'ab'")]
    assert _kinds(diff_columns(src, tgt)) == {"n": [ColumnChangeKind.DEFAULT_CHANGED]}


def test_default_none_versus_empty_string_differs() -> None:
    src = [_col("n", "int", default=None)]
    tgt = [_col("n", "int", default="''")]
    assert _kinds(diff_columns(src, tgt)) == {"n": [ColumnChangeKind.DEFAULT_CHANGED]}


def test_type_synonyms_are_not_type_change() -> None:
    """S2 canonicalization pays off here: both sides render through one canonical form."""
    assert diff_columns([_col("n", "int")], [_col("n", "int")]) == []
    assert diff_columns([_col("c", "char(3)")], [_col("c", "char(3)")]) == []


# ------------------------------------------------------- comparator integration


def _obj(key: str, sql_hash: str, object_type: str = "table",
         columns: list[ColumnSnapshot] | None = None) -> ObjectSnapshot:
    return ObjectSnapshot(
        object_key=key,
        object_schema="app",
        object_name=key.split("name/")[-1],
        object_type=object_type,
        sql_normalized="CREATE TABLE t (a INT)",
        sql_hash=sql_hash,
        columns=columns,
    )


def _state(objects: dict[str, ObjectSnapshot]) -> StateSnapshot:
    return StateSnapshot(
        source_kind=SnapshotSourceKind.DIR,
        source_ref="src",
        db_type="postgres",
        generated_at="2026-08-16T00:00:00+00:00",
        objects=objects,
        edges=[],
    )


def test_changed_table_gets_column_diffs() -> None:
    src = _state({"k": _obj("k", "h1", columns=[_col("a"), _col("b", "text")])})
    tgt = _state({"k": _obj("k", "h2", columns=[_col("a"), _col("b", "int")])})
    entry = next(e for e in compare(src, tgt).entries if e.status is DiffStatus.CHANGED)
    assert entry.column_diffs
    assert _kinds(entry.column_diffs) == {"b": [ColumnChangeKind.TYPE_CHANGED]}
    assert entry.columns_unavailable is False


def test_changed_table_source_columns_none_marks_unavailable() -> None:
    src = _state({"k": _obj("k", "h1", columns=None)})
    tgt = _state({"k": _obj("k", "h2", columns=[_col("a")])})
    entry = next(e for e in compare(src, tgt).entries if e.status is DiffStatus.CHANGED)
    assert entry.columns_unavailable is True
    assert entry.column_diffs == []


def test_changed_table_target_columns_none_marks_unavailable() -> None:
    src = _state({"k": _obj("k", "h1", columns=[_col("a")])})
    tgt = _state({"k": _obj("k", "h2", columns=None)})
    entry = next(e for e in compare(src, tgt).entries if e.status is DiffStatus.CHANGED)
    assert entry.columns_unavailable is True


def test_changed_non_table_has_no_column_diffs() -> None:
    src = _state({"k": _obj("k", "h1", object_type="view", columns=None)})
    tgt = _state({"k": _obj("k", "h2", object_type="view", columns=None)})
    entry = next(e for e in compare(src, tgt).entries if e.status is DiffStatus.CHANGED)
    assert entry.column_diffs == []
    assert entry.columns_unavailable is False


def test_unchanged_table_has_no_column_diffs_computed() -> None:
    src = _state({"k": _obj("k", "h1", columns=[_col("a")])})
    tgt = _state({"k": _obj("k", "h1", columns=[_col("a", "bigint")])})
    entry = next(e for e in compare(src, tgt).entries if e.status is DiffStatus.UNCHANGED)
    assert entry.column_diffs == []


def test_columns_unavailable_roundtrip() -> None:
    """DiffReport with the new fields survives pydantic round-trip (LESSONS §28)."""
    src = _state({"k": _obj("k", "h1", columns=None)})
    tgt = _state({"k": _obj("k", "h2", columns=[_col("a")])})
    report = compare(src, tgt)
    restored = type(report).model_validate_json(report.model_dump_json())
    entry = next(e for e in restored.entries if e.status is DiffStatus.CHANGED)
    assert entry.columns_unavailable is True
    assert entry.source_snapshot is not None
    assert entry.source_snapshot.columns is None

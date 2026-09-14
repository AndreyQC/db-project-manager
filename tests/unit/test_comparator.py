"""Unit tests for the comparator (Phase 9)."""

from __future__ import annotations

from db_project_manager.domain.delta import ColumnSnapshot
from db_project_manager.domain.diff import (
    DiffStatus,
    EdgeSnapshot,
    ObjectSnapshot,
    SnapshotSourceKind,
    StateSnapshot,
)
from db_project_manager.infrastructure.diff.comparator import compare


def _obj(key: str, sql_hash: str = "aaaa0000", **overrides) -> ObjectSnapshot:
    base = {
        "object_key": key,
        "object_schema": "public",
        "object_name": key.split("name/")[-1],
        "object_type": "table",
        "sql_normalized": "CREATE TABLE t (a INT)",
        "sql_hash": sql_hash,
    }
    base.update(overrides)
    return ObjectSnapshot(**base)


def _state(objects: dict[str, ObjectSnapshot], **overrides) -> StateSnapshot:
    base = {
        "source_kind": SnapshotSourceKind.DIR,
        "source_ref": "src",
        "db_type": "postgres",
        "generated_at": "2026-07-29T00:00:00+00:00",
        "objects": objects,
        "edges": [],
    }
    base.update(overrides)
    return StateSnapshot(**base)


# --- single-status scenarios ---


def test_all_unchanged_when_identical_hashes():
    src = _state({"k1": _obj("k1"), "k2": _obj("k2", "bbbb0000")})
    tgt = _state({"k1": _obj("k1"), "k2": _obj("k2", "bbbb0000")})
    report = compare(src, tgt)
    assert report.summary == {"added": 0, "removed": 0, "changed": 0, "unchanged": 2}


def test_all_added_when_target_empty():
    src = _state({"k1": _obj("k1")})
    tgt = _state({})
    report = compare(src, tgt)
    assert report.summary == {"added": 1, "removed": 0, "changed": 0, "unchanged": 0}
    assert report.entries[0].status == DiffStatus.ADDED
    assert report.entries[0].source_snapshot is not None
    assert report.entries[0].target_snapshot is None


def test_all_removed_when_source_empty():
    src = _state({})
    tgt = _state({"k1": _obj("k1")})
    report = compare(src, tgt)
    assert report.summary == {"added": 0, "removed": 1, "changed": 0, "unchanged": 0}
    assert report.entries[0].status == DiffStatus.REMOVED
    assert report.entries[0].target_snapshot is not None
    assert report.entries[0].source_snapshot is None


def test_changed_when_hash_differs():
    src = _state({"k1": _obj("k1", "aaaa0000")})
    tgt = _state({"k1": _obj("k1", "bbbb0000")})
    report = compare(src, tgt)
    assert report.summary == {"added": 0, "removed": 0, "changed": 1, "unchanged": 0}
    entry = report.entries[0]
    assert entry.status == DiffStatus.CHANGED
    assert entry.source_snapshot is not None
    assert entry.target_snapshot is not None


# --- mixed scenario ---


def test_mixed_scenario_one_of_each_status():
    src = _state({
        "added_only": _obj("added_only"),
        "changed": _obj("changed", "aaaa0000"),
        "unchanged": _obj("unchanged", "cccc0000"),
    })
    tgt = _state({
        "changed": _obj("changed", "bbbb0000"),
        "unchanged": _obj("unchanged", "cccc0000"),
        "removed_only": _obj("removed_only"),
    })
    report = compare(src, tgt)
    assert report.summary == {"added": 1, "removed": 1, "changed": 1, "unchanged": 1}


# --- edge cases ---


def test_both_empty_snapshots():
    report = compare(_state({}), _state({}))
    assert report.summary == {"added": 0, "removed": 0, "changed": 0, "unchanged": 0}
    assert report.entries == []


def test_overloads_distinct_by_signature_key():
    """Two overloads (same name, different object_key) are compared independently."""
    src = _state({
        "pg/db/type/function/name/sp_x/signature/aaa": _obj("pg/db/type/function/name/sp_x/signature/aaa"),
    })
    tgt = _state({
        "pg/db/type/function/name/sp_x/signature/bbb": _obj("pg/db/type/function/name/sp_x/signature/bbb"),
    })
    report = compare(src, tgt)
    # Different keys → one added (in src), one removed (in tgt).
    assert report.summary["added"] == 1
    assert report.summary["removed"] == 1
    assert report.summary["changed"] == 0


def test_report_carries_snapshots_and_timestamp():
    src = _state({"k1": _obj("k1")})
    tgt = _state({"k1": _obj("k1")})
    report = compare(src, tgt)
    assert report.source is src
    assert report.target is tgt
    assert report.generated_at  # ISO timestamp present


# --- edge diff (Phase 14) ---


def _edge(src_key: str, dst_key: str, relation: str = "depends_on",
          action: str = "references") -> EdgeSnapshot:
    return EdgeSnapshot(
        source_object_key=src_key,
        destination_object_key=dst_key,
        relation=relation,
        action=action,
    )


def test_edge_diff_added_removed():
    """Edges present in source-only are added; target-only are removed."""
    src = _state({}, edges=[_edge("a", "b"), _edge("a", "c")])
    tgt = _state({}, edges=[_edge("a", "b"), _edge("d", "a")])
    report = compare(src, tgt)
    assert report.edge_summary == {"added": 1, "removed": 1}
    statuses = sorted(e.status.value for e in report.edge_entries)
    assert statuses == ["added", "removed"]
    added = next(e for e in report.edge_entries if e.status is DiffStatus.ADDED)
    assert added.source_edge is not None
    assert added.source_edge.destination_object_key == "c"
    removed = next(e for e in report.edge_entries if e.status is DiffStatus.REMOVED)
    assert removed.target_edge is not None
    assert removed.target_edge.source_object_key == "d"


def test_edge_diff_empty_when_identical_edges():
    src = _state({}, edges=[_edge("a", "b"), _edge("a", "c")])
    tgt = _state({}, edges=[_edge("a", "c"), _edge("a", "b")])  # same set, diff order
    report = compare(src, tgt)
    assert report.edge_summary == {"added": 0, "removed": 0}
    assert report.edge_entries == []


def test_edge_diff_uses_action_in_identity():
    """Same endpoints but different action → different edges (both added and removed)."""
    src = _state({}, edges=[_edge("a", "b", action="select")])
    tgt = _state({}, edges=[_edge("a", "b", action="references")])
    report = compare(src, tgt)
    assert report.edge_summary == {"added": 1, "removed": 1}


def test_edge_diff_no_edges_when_both_empty():
    report = compare(_state({}), _state({}))
    assert report.edge_summary == {"added": 0, "removed": 0}


def test_backward_compat_state_without_edges_field():
    """A StateSnapshot dict missing the 'edges' key parses to an empty edge list.

    Old ``source.json``/``target.json`` from Phase 9 have no ``edges`` field; the
    comparator must not crash and must produce an empty edge diff.
    """
    raw_state = {
        "source_kind": "dir",
        "source_ref": "src",
        "db_type": "postgres",
        "generated_at": "2026-07-29T00:00:00+00:00",
        "objects": {},
    }  # no 'edges' key
    src = StateSnapshot.model_validate(raw_state)
    tgt = StateSnapshot.model_validate(raw_state)
    report = compare(src, tgt)
    assert report.edge_entries == []


def test_compare_ignores_catalog_segment_in_object_key():
    """Phase 12 rehearsal finding: the same object RE'd from differently-NAMED
    databases must compare as unchanged — the DB name is environment-specific,
    not identity (schema/type/name/signature is)."""
    src_key = "pg_database/prod_db/schema/app/type/table/name/orders"
    tgt_key = "pg_database/dbpm_rehearsal_x/schema/app/type/table/name/orders"
    src = _state({src_key: _obj(src_key)})
    tgt = _state({tgt_key: _obj(tgt_key)})
    report = compare(src, tgt)
    assert report.summary == {"added": 0, "removed": 0, "changed": 0, "unchanged": 1}
    # entry carries the source-side (codebase) key — that's what the plan matches
    assert report.entries[0].object_key == src_key


def test_compare_catalog_insensitive_added_removed():
    src = _state({"pg_database/dev_db/schema/app/type/table/name/new_t": _obj("k")})
    tgt = _state({"pg_database/prod_db/schema/app/type/table/name/old_t": _obj("k")})
    report = compare(src, tgt)
    statuses = {e.object_key.rsplit("/", 1)[-1]: e.status for e in report.entries}
    assert statuses["new_t"] is DiffStatus.ADDED
    assert statuses["old_t"] is DiffStatus.REMOVED


# --- Phase 15.5.5: sql_hash differs but column-diffs are empty -> UNCHANGED ---


def test_hash_differs_but_columns_canonicalize_equal_is_unchanged():
    """Phase 15.5.5 (cis_zup feedback 2026-09-04): sql_hash is computed by
    normalize_sql which preserves the lexical form of types (serial4 vs
    int4 are distinct text). Phase 15.5.4 collapses them inside
    ColumnSnapshot.type and via the nextval default-equivalence rule. The
    comparator must therefore trust the column-level signal: when
    diff_columns() yields no diffs, declare UNCHANGED.

    Without 15.5.5 the gate would still see CHANGED even though the column
    diff is empty, because sql_hash kept the serial4 vs int4 difference.
    """
    from db_project_manager.infrastructure.diff.columns import diff_columns

    src_obj = _obj(
        "k1",
        sql_hash="hash_from_source_with_serial4",
        columns=[
            ColumnSnapshot(name="id", type="int", nullable=False, default=None),
            ColumnSnapshot(name="n", type="text", nullable=True, default=None),
        ],
    )
    tgt_obj = _obj(
        "k1",
        sql_hash="different_hash_with_int4_and_nextval",
        columns=[
            ColumnSnapshot(name="id", type="int", nullable=False, default="NEXTVAL(CAST('s.id_seq' AS REGCLASS))"),
            ColumnSnapshot(name="n", type="text", nullable=True, default=None),
        ],
    )
    src = _state({"k1": src_obj})
    tgt = _state({"k1": tgt_obj})

    # Sanity: Phase 15.5.4 makes column diffs empty.
    assert diff_columns(src_obj.columns, tgt_obj.columns) == []

    report = compare(src, tgt)
    statuses = {e.object_key: e.status for e in report.entries}
    assert statuses["k1"] is DiffStatus.UNCHANGED, (
        f"serial4↔int+nextval must be UNCHANGED via column-level compensation; "
        f"got {statuses!r}, report.summary={report.summary}"
    )
    # No spurious CHANGED in summary either.
    assert report.summary["changed"] == 0
    assert report.summary["unchanged"] == 1


def test_hash_differs_and_columns_really_differ_is_changed():
    """The opposite: when column diffs are non-empty (a real schema change),
    CHANGED status stays — even after 15.5.5 compensation, real changes
    remain visible."""
    src_obj = _obj(
        "k1",
        sql_hash="hash_src",
        columns=[
            ColumnSnapshot(name="a", type="int", nullable=False, default=None),
        ],
    )
    tgt_obj = _obj(
        "k1",
        sql_hash="hash_tgt_different",
        columns=[
            # Default value differs — a real semantic change, not serial/int round-trip.
            ColumnSnapshot(name="a", type="int", nullable=False, default="42"),
        ],
    )
    src = _state({"k1": src_obj})
    tgt = _state({"k1": tgt_obj})

    report = compare(src, tgt)
    statuses = {e.object_key: e.status for e in report.entries}
    assert statuses["k1"] is DiffStatus.CHANGED
    assert report.summary["changed"] == 1


def test_hash_agrees_is_unchanged_even_with_fake_columns():
    """Regression of the simpler path: identical hash always wins, regardless
    of column content (the underlying objects are still equal).
    """
    src_obj = _obj("k1", sql_hash="same", columns=None)
    tgt_obj = _obj("k1", sql_hash="same", columns=None)
    src = _state({"k1": src_obj})
    tgt = _state({"k1": tgt_obj})
    report = compare(src, tgt)
    assert report.summary["unchanged"] == 1


def test_columns_unavailable_keeps_changed_even_with_compensation():
    """If either side has columns=None (parse failure), we cannot rely on
    column compensation — keep the legacy CHANGED-on-sql-hash-diff behaviour.
    """
    src_obj = _obj("k1", sql_hash="hash_src", columns=None)
    tgt_obj = _obj("k1", sql_hash="hash_tgt_different", columns=None)
    src = _state({"k1": src_obj})
    tgt = _state({"k1": tgt_obj})
    report = compare(src, tgt)
    statuses = {e.object_key: e.status for e in report.entries}
    assert statuses["k1"] is DiffStatus.CHANGED, (
        "without column diffs available, sql_hash is the only signal — keep "
        "the conservative CHANGED to satisfy fail-safe contracts"
    )

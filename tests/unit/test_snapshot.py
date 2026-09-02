"""Unit tests for the snapshot builder (Phase 9)."""

from __future__ import annotations

from pathlib import Path

from db_project_manager.domain.diff import SnapshotSourceKind
from db_project_manager.infrastructure.diff.snapshot import (
    DIFFED_TYPES,
    build_snapshot_from_dir,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
CODEBASE_SAMPLE = FIXTURES / "codebase_sample"


def test_snapshot_filters_to_diffed_types():
    """Extensions and database_settings are excluded; only DIFFED_TYPES appear."""
    snap = build_snapshot_from_dir(
        CODEBASE_SAMPLE,
        source_kind=SnapshotSourceKind.DIR,
        source_ref=str(CODEBASE_SAMPLE),
        db_type="postgres",
    )
    types = {o.object_type for o in snap.objects.values()}
    assert types <= DIFFED_TYPES
    assert "extension" not in types
    assert "database_setting" not in types


def test_snapshot_contains_expected_object_count():
    """Fixture has 20 vertices; minus extension & database_setting = 18.

    Regression note: 2026-09-02 — ``schema`` was added to DIFFED_TYPES after the
    cis_zup feedback (Phase 15.5). Before the fix, schema vertices were silently
    dropped from snapshots, so ``deploy apply`` on an empty target DB tried to
    CREATE TABLE in a schema that did not yet exist (InvalidSchemaName). With
    the fix, 3 user/service schemas enter the snapshot: __deploy, app, bookings.
    """
    snap = build_snapshot_from_dir(
        CODEBASE_SAMPLE,
        source_kind=SnapshotSourceKind.DIR,
        source_ref=str(CODEBASE_SAMPLE),
        db_type="postgres",
    )
    # 20 vertices total: -1 extension -1 database_setting +0 (schemas now kept)
    # = 18 diffed.
    assert len(snap.objects) == 18


def test_snapshot_includes_schema_vertices():
    """Regression test for cis_zup feedback 2026-09-02.

    ``schema`` MUST be a member of DIFFED_TYPES — otherwise CompareService
    drops schema vertices from the snapshot, the resulting DeltaPlan has no
    CREATE SCHEMA artifact, and deploy apply on an empty target fails with
    ``psycopg2.errors.InvalidSchemaName: schema "X" does not exist`` when
    CREATE TABLE "X"."t" tries to run first.
    """
    assert "schema" in DIFFED_TYPES, (
        "schema must be in DIFFED_TYPES (Phase 15.5 regression after cis_zup "
        "feedback 2026-09-02): without it, schema vertices are dropped from "
        "snapshots and CREATE SCHEMA artifacts are never emitted, breaking "
        "deploy apply on empty target DBs."
    )
    snap = build_snapshot_from_dir(
        CODEBASE_SAMPLE,
        source_kind=SnapshotSourceKind.DIR,
        source_ref=str(CODEBASE_SAMPLE),
        db_type="postgres",
    )
    schema_keys = [k for k, v in snap.objects.items() if v.object_type == "schema"]
    assert len(schema_keys) >= 2, (
        f"Expected at least __deploy + user schemas in snapshot, got {schema_keys}"
    )
    for k in schema_keys:
        obj = snap.objects[k]
        assert obj.sql_hash, "schema snapshot must have a non-empty sql_hash"
        assert obj.sql_normalized, "schema snapshot must have a non-empty sql_normalized"


def test_each_object_has_sql_hash():
    snap = build_snapshot_from_dir(
        CODEBASE_SAMPLE,
        source_kind=SnapshotSourceKind.DIR,
        source_ref=str(CODEBASE_SAMPLE),
        db_type="postgres",
    )
    for o in snap.objects.values():
        assert len(o.sql_hash) == 8
        assert o.sql_normalized  # non-empty body


def test_row_counts_attached_to_tables_only():
    """Tables get estimated_rows from row_counts; views/functions stay None."""
    # Provide a row count for one table in the fixture (bookings.aircrafts).
    row_counts = {("bookings", "aircrafts"): 8}
    snap = build_snapshot_from_dir(
        CODEBASE_SAMPLE,
        source_kind=SnapshotSourceKind.DIR,
        source_ref=str(CODEBASE_SAMPLE),
        db_type="postgres",
        row_counts=row_counts,
    )
    aircrafts = next(
        o for o in snap.objects.values()
        if o.object_type == "table" and o.object_name == "aircrafts"
    )
    assert aircrafts.estimated_rows == 8
    # Other tables without a row count stay None.
    other_tables = [
        o for o in snap.objects.values()
        if o.object_type == "table" and o.object_name != "aircrafts"
    ]
    assert other_tables, "fixture should have other tables"
    assert all(o.estimated_rows is None for o in other_tables)
    # Non-table objects never get a row count even if a matching name existed.
    non_tables = [o for o in snap.objects.values() if o.object_type != "table"]
    assert all(o.estimated_rows is None for o in non_tables)


def test_snapshot_metadata_propagated():
    snap = build_snapshot_from_dir(
        CODEBASE_SAMPLE,
        source_kind=SnapshotSourceKind.DB,
        source_ref="my_connection",
        db_type="greenplum",
    )
    assert snap.source_kind == SnapshotSourceKind.DB
    assert snap.source_ref == "my_connection"
    assert snap.db_type == "greenplum"
    assert snap.generated_at  # ISO timestamp


def test_empty_codebase_yields_empty_snapshot(tmp_path):
    """A directory with no .sql files produces an empty snapshot, no error."""
    snap = build_snapshot_from_dir(
        tmp_path,
        source_kind=SnapshotSourceKind.DIR,
        source_ref=str(tmp_path),
        db_type="postgres",
    )
    assert snap.objects == {}


def test_overloaded_functions_get_distinct_keys():
    """Two overloads (same name, different signature) must have distinct object_keys."""
    snap = build_snapshot_from_dir(
        CODEBASE_SAMPLE,
        source_kind=SnapshotSourceKind.DIR,
        source_ref=str(CODEBASE_SAMPLE),
        db_type="postgres",
    )
    sp_x = [o for o in snap.objects.values() if o.object_name == "sp_x"]
    assert len(sp_x) == 2
    assert len({o.object_key for o in sp_x}) == 2
    assert len({o.object_signature for o in sp_x}) == 2


def test_snapshot_is_deterministic_across_runs():
    """Running twice produces identical sql_hash for each object."""
    snap1 = build_snapshot_from_dir(
        CODEBASE_SAMPLE,
        source_kind=SnapshotSourceKind.DIR,
        source_ref=str(CODEBASE_SAMPLE),
        db_type="postgres",
    )
    snap2 = build_snapshot_from_dir(
        CODEBASE_SAMPLE,
        source_kind=SnapshotSourceKind.DIR,
        source_ref=str(CODEBASE_SAMPLE),
        db_type="postgres",
    )
    for key in snap1.objects:
        assert snap1.objects[key].sql_hash == snap2.objects[key].sql_hash

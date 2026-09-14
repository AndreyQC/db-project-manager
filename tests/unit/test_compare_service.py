"""Unit tests for CompareService orchestration (Phase 9)."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from db_project_manager.application.compare_service import (
    DIFF_REPORT_FILENAME,
    SOURCE_FILENAME,
    TARGET_FILENAME,
    CompareError,
    CompareService,
    SideSpec,
)
from db_project_manager.domain.connection import ConnectionConfig
from db_project_manager.domain.diff import CodebaseManifest, SnapshotSourceKind
from db_project_manager.infrastructure.config.codebase_manifest import write_manifest

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
CODEBASE_SAMPLE = FIXTURES / "codebase_sample"


def _conn(db: str = "mydb", db_type: str = "postgres") -> ConnectionConfig:
    return ConnectionConfig(
        host="localhost", port=5432, database=db, username="u", password="p", type=db_type
    )


def _copy_fixture_with_manifest(tmp_path: Path, db_type: str = "postgres") -> Path:
    """Copy the codebase_sample fixture and write a manifest with a given db_type."""
    dest = tmp_path / "codebase"
    shutil.copytree(CODEBASE_SAMPLE, dest)
    write_manifest(
        CodebaseManifest(
            db_type=db_type,
            database="demo",
            generated_at="2026-07-29T00:00:00+00:00",
            source_version="2026.07.29.01",  # Phase 10: required at v2
        ),
        dest,
    )
    return dest


class _StubReverseEngineer:
    """Reverse-engineer stub: copies the fixture tree into the temp output and writes a manifest."""

    def __init__(self, db_type: str = "postgres", db_name: str = "mydb") -> None:
        self.db_type = db_type
        self.db_name = db_name

    def run(self, conn_cfg: ConnectionConfig, output_dir: str | Path, progress=None) -> Path:  # noqa: ARG002
        out = Path(output_dir) / self.db_name
        shutil.copytree(CODEBASE_SAMPLE, out)
        write_manifest(
            CodebaseManifest(
                db_type=self.db_type,
                database=self.db_name,
                generated_at="2026-07-29T00:00:00+00:00",
                source_version="2026.07.29.01",  # Phase 10: required at v2
            ),
            out,
        )
        return out


def _fake_adapter_factory(rows: list[dict[str, Any]] | None = None):
    """Build an adapter factory whose get_table_row_counts returns the given rows."""

    class _Adapter:
        def __init__(self) -> None:
            self.connected = False

        def connect(self, cfg: ConnectionConfig) -> None:  # noqa: ARG002
            self.connected = True

        def disconnect(self) -> None:
            self.connected = False

        def get_table_row_counts(self) -> list[dict[str, Any]]:
            return rows or []

    adapter = _Adapter()
    return lambda _cfg: adapter


# --- DIR vs DIR ---


def test_dir_vs_dir_identical_produces_all_unchanged(tmp_path):
    src = _copy_fixture_with_manifest(tmp_path / "src")
    tgt = _copy_fixture_with_manifest(tmp_path / "tgt")
    out = tmp_path / "report"

    service = CompareService()
    result = service.run(
        SideSpec(SnapshotSourceKind.DIR, str(src)),
        SideSpec(SnapshotSourceKind.DIR, str(tgt)),
        out,
    )
    assert result == out
    assert (out / SOURCE_FILENAME).is_file()
    assert (out / TARGET_FILENAME).is_file()
    report_text = (out / DIFF_REPORT_FILENAME).read_text(encoding="utf-8")
    # Identical fixtures → everything unchanged, nothing added/removed/changed.
    # Phase 15.5 (cis_zup feedback 2026-09-02): ``schema`` was added to
    # DIFFED_TYPES, so schemas are now diffed too. The fixture has 20 vertices:
    # -1 extension -1 database_setting = 18 diffed (15 non-schema + 3 schemas).
    # Phase 15.7: ``materialized_view routes`` has project.build=false in its
    # autodoc — excluded from the diff on BOTH sides → 18 - 1 = 17 unchanged.
    assert '"unchanged": 17' in report_text
    assert '"added": 0' in report_text
    assert '"ignored_build_false": 1' in report_text


def test_dir_vs_dir_missing_manifest_raises(tmp_path):
    # Copy without writing a manifest.
    dest = tmp_path / "no_manifest"
    shutil.copytree(CODEBASE_SAMPLE, dest)
    # Phase 10: fixture ships with a manifest now — remove it for this test.
    (dest / "dbpm.manifest.json").unlink()
    out = tmp_path / "report"

    service = CompareService()
    with pytest.raises(CompareError, match="не содержит"):
        service.run(
            SideSpec(SnapshotSourceKind.DIR, str(dest)),
            SideSpec(SnapshotSourceKind.DIR, str(dest)),
            out,
        )


def test_dir_vs_dir_db_type_mismatch_raises(tmp_path):
    src = _copy_fixture_with_manifest(tmp_path / "src", db_type="postgres")
    tgt = _copy_fixture_with_manifest(tmp_path / "tgt", db_type="greenplum")
    out = tmp_path / "report"

    service = CompareService()
    with pytest.raises(CompareError, match="Несовместимые типы БД"):
        service.run(
            SideSpec(SnapshotSourceKind.DIR, str(src)),
            SideSpec(SnapshotSourceKind.DIR, str(tgt)),
            out,
        )


# --- DB side via stub reverse-engineer ---


def test_db_side_via_stub_reverse_engineer(tmp_path):
    """A DB side reverse-engineers into a temp dir (stub) and is cleaned up."""
    out = tmp_path / "report"
    re_stub = _StubReverseEngineer(db_type="postgres", db_name="mydb")
    conn = _conn(db="mydb", db_type="postgres")

    service = CompareService(
        reverse_engineer=re_stub,  # type: ignore[arg-type]
        adapter_factory=_fake_adapter_factory(),
    )
    # DB vs DB (both stub) — same db_type, should succeed.
    result = service.run(
        SideSpec(SnapshotSourceKind.DB, "conn.yaml", conn_cfg=conn),
        SideSpec(SnapshotSourceKind.DB, "conn2.yaml", conn_cfg=conn),
        out,
    )
    assert (result / DIFF_REPORT_FILENAME).is_file()


def test_temp_dir_cleaned_up_by_default(tmp_path):
    """Without keep_model_dir, the reverse-engineer temp dir is removed after run."""
    re_stub = _StubReverseEngineer(db_type="postgres", db_name="mydb")
    conn = _conn(db="mydb")
    created_dirs: list[Path] = []
    orig_run = re_stub.run

    def spy_run(conn_cfg, output_dir, progress=None):  # noqa: ARG001
        result = orig_run(conn_cfg, output_dir, progress)
        created_dirs.append(Path(output_dir))
        return result

    re_stub.run = spy_run  # type: ignore[assignment]

    service = CompareService(
        reverse_engineer=re_stub,  # type: ignore[arg-type]
        adapter_factory=_fake_adapter_factory(),
    )
    service.run(
        SideSpec(SnapshotSourceKind.DB, "conn.yaml", conn_cfg=conn),
        SideSpec(SnapshotSourceKind.DB, "conn2.yaml", conn_cfg=conn),
        tmp_path / "report",
    )
    # Temp dirs were created and then removed (cleanup in finally).
    assert created_dirs, "stub should have been called"
    assert all(not d.exists() for d in created_dirs)


def test_temp_dir_kept_with_keep_model_dir(tmp_path):
    """With keep_model_dir=True, the reverse-engineer temp dir survives."""
    re_stub = _StubReverseEngineer(db_type="postgres", db_name="mydb")
    conn = _conn(db="mydb")
    created_dirs: list[Path] = []

    orig_run = re_stub.run

    def spy_run(conn_cfg, output_dir, progress=None):  # noqa: ARG001
        created_dirs.append(Path(output_dir))
        return orig_run(conn_cfg, output_dir, progress)

    re_stub.run = spy_run  # type: ignore[assignment]

    service = CompareService(
        reverse_engineer=re_stub,  # type: ignore[arg-type]
        adapter_factory=_fake_adapter_factory(),
    )
    service.run(
        SideSpec(SnapshotSourceKind.DB, "conn.yaml", conn_cfg=conn),
        SideSpec(SnapshotSourceKind.DB, "conn2.yaml", conn_cfg=conn),
        tmp_path / "report",
        keep_model_dir=True,
    )
    assert created_dirs
    assert all(d.exists() for d in created_dirs)


def test_db_type_mismatch_between_db_and_dir(tmp_path):
    """PG DB side vs GP dir side → CompareError."""
    tgt = _copy_fixture_with_manifest(tmp_path / "tgt", db_type="greenplum")
    conn = _conn(db="mydb", db_type="postgres")
    out = tmp_path / "report"

    service = CompareService(
        reverse_engineer=_StubReverseEngineer(db_type="postgres", db_name="mydb"),  # type: ignore[arg-type]
        adapter_factory=_fake_adapter_factory(),
    )
    with pytest.raises(CompareError, match="Несовместимые типы БД"):
        service.run(
            SideSpec(SnapshotSourceKind.DB, "conn.yaml", conn_cfg=conn),
            SideSpec(SnapshotSourceKind.DIR, str(tgt)),
            out,
        )


def test_db_side_snapshot_copied_into_run_dir(tmp_path):
    """Phase 15.7 (BACKLOG P3): DB-side RE snapshots survive in target/ and source/."""
    out = tmp_path / "report"
    conn = _conn(db="mydb")

    service = CompareService(
        reverse_engineer=_StubReverseEngineer(db_type="postgres", db_name="mydb"),  # type: ignore[arg-type]
        adapter_factory=_fake_adapter_factory(),
    )
    service.run(
        SideSpec(SnapshotSourceKind.DB, "conn.yaml", conn_cfg=conn),
        SideSpec(SnapshotSourceKind.DB, "conn2.yaml", conn_cfg=conn),
        out,
    )
    assert (out / "source" / "mydb" / "dbpm.manifest.json").is_file()
    assert (out / "target" / "mydb" / "dbpm.manifest.json").is_file()


def test_build_false_excluded_from_both_sides(tmp_path):
    """Phase 15.7: build=false on ONE side excludes the object from both sides.

    Source (DIR) keeps ``materialized_view routes`` at build=false; target (DIR)
    flips it to build=true. Without two-sided exclusion the object would surface
    as REMOVED (present in target, absent from a source-only filter). It must
    instead vanish entirely, with a documented ignored count.
    """
    src = _copy_fixture_with_manifest(tmp_path / "src")
    tgt = _copy_fixture_with_manifest(tmp_path / "tgt")
    routes = tgt / "bookings" / "materialized_views" / "materialized_view routes.sql"
    text = routes.read_text(encoding="utf-8-sig").replace("build: false", "build: true")
    routes.write_text(text, encoding="utf-8")

    out = tmp_path / "report"
    service = CompareService()
    service.run(
        SideSpec(SnapshotSourceKind.DIR, str(src)),
        SideSpec(SnapshotSourceKind.DIR, str(tgt)),
        out,
    )

    import json

    report = json.loads((out / DIFF_REPORT_FILENAME).read_text(encoding="utf-8"))
    assert report["summary"]["ignored_build_false"] == 1
    assert not any("routes" in e["object_key"] for e in report["entries"])
    # The build=false object must not leak into the diff as removed.
    assert report["summary"]["removed"] == 0

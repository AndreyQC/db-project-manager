"""Tests for SafetyGateService (Phase 11, step S5).

Fixtures: a DeployFakeAdapter with configurable ``presence_stats`` /
``_schema_version``; a stub compare service that writes a synthetic
``diff_report.json`` (the real hand-off contract); a minimal codebase dir with
``dbpm.manifest.json`` built via ``write_manifest``. The read-only contract
(no mutating adapter calls) is asserted explicitly.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from db_project_manager.application.compare_service import DIFF_REPORT_FILENAME
from db_project_manager.application.safety_gate_service import (
    SafetyGateError,
    SafetyGateService,
)
from db_project_manager.domain.connection import ConnectionConfig
from db_project_manager.domain.diff import (
    DiffEntry,
    DiffReport,
    DiffStatus,
    ObjectSnapshot,
    SnapshotSourceKind,
    StateSnapshot,
)
from db_project_manager.domain.safety import StatsConfidence, TablePresenceStats
from db_project_manager.infrastructure.config.codebase_manifest import (
    CodebaseManifest,
    write_manifest,
)
from tests.unit.test_deploy_service import DeployFakeAdapter


def _cfg(db_type: str = "postgres") -> ConnectionConfig:
    return ConnectionConfig(
        host="localhost", port=5432, database="target", username="u",
        password="p", type=db_type,
    )


def _codebase(tmp_path: Path, *, db_type: str = "postgres",
              source_version: str = "2026.08.14.01") -> Path:
    root = tmp_path / "codebase"
    root.mkdir(parents=True, exist_ok=True)
    write_manifest(
        CodebaseManifest(
            db_type=db_type, database="demo", generated_at="2026-08-14T00:00:00+00:00",
            source_version=source_version,
        ),
        root,
    )
    return root


def _snap(obj_type: str, schema: str, name: str, key: str | None = None,
          sql_hash: str = "aaaa0000") -> ObjectSnapshot:
    return ObjectSnapshot(
        object_key=key or f"pg_database/demo/schema/{schema}/type/{obj_type}/name/{name}",
        object_schema=schema, object_name=name, object_type=obj_type,
        sql_normalized="SELECT 1", sql_hash=sql_hash,
    )


def _report(entries: list[DiffEntry]) -> DiffReport:
    src = StateSnapshot(
        source_kind=SnapshotSourceKind.DIR, source_ref="code",
        db_type="postgres", generated_at="2026-08-14T00:00:00+00:00",
        objects={e.object_key: s for e in entries if (s := e.source_snapshot)},
    )
    tgt = StateSnapshot(
        source_kind=SnapshotSourceKind.DB, source_ref="target",
        db_type="postgres", generated_at="2026-08-14T00:00:00+00:00",
        objects={e.object_key: s for e in entries if (s := e.target_snapshot)},
    )
    counts: dict[str, int] = {}
    for e in entries:
        counts[e.status.value] = counts.get(e.status.value, 0) + 1
    return DiffReport(
        source=src, target=tgt,
        generated_at="2026-08-14T00:00:00+00:00",
        summary=counts, entries=entries,
    )


class _StubCompare:
    """Writes the pre-built report to output_dir — the real hand-off contract."""

    def __init__(self, report: DiffReport) -> None:
        self.report = report
        self.calls: list[dict[str, Any]] = []

    def run(self, source, target, output_dir, *, keep_model_dir=False, progress=None):  # noqa: ARG002
        self.calls.append({"source": source, "target": target})
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / DIFF_REPORT_FILENAME).write_text(
            self.report.model_dump_json(indent=2), encoding="utf-8"
        )
        return output_dir


def _analyze(tmp_path: Path, adapter: DeployFakeAdapter, stub: _StubCompare,
             cfg: ConnectionConfig | None = None, codebase: Path | None = None):
    service = SafetyGateService(
        adapter_factory=lambda _cfg: adapter, compare_service=stub,
    )
    return service.analyze(
        codebase or _codebase(tmp_path), cfg or _cfg(), tmp_path / "report"
    )


def _stats(schema: str, name: str, rows: int | None,
           confidence: StatsConfidence = StatsConfidence.FRESH) -> TablePresenceStats:
    return TablePresenceStats(
        object_schema=schema, name=name, estimated_rows=rows, confidence=confidence
    )


def _changed_entry(schema: str = "app", name: str = "orders") -> DiffEntry:
    snap = _snap("table", schema, name)
    return DiffEntry(
        object_key=snap.object_key, status=DiffStatus.CHANGED,
        source_snapshot=snap, target_snapshot=_snap("table", schema, name, sql_hash="bbbb1111"),
    )


# ------------------------------------------------------------------ hard errors


def test_db_type_mismatch_fails_fast(tmp_path: Path) -> None:
    adapter = DeployFakeAdapter()
    stub = _StubCompare(_report([]))
    with pytest.raises(SafetyGateError, match="не совпадает"):
        _analyze(tmp_path, adapter, stub, cfg=_cfg("snowflake"))
    assert stub.calls == []  # fail-fast: compare never ran (no wasted RE)


def test_target_newer_version_raises(tmp_path: Path) -> None:
    adapter = DeployFakeAdapter()
    adapter._schema_versions.append(("__deploy", "2026.09.01.01", "deploy"))
    stub = _StubCompare(_report([]))
    with pytest.raises(SafetyGateError, match="новее"):
        _analyze(tmp_path, adapter, stub)


def test_same_version_proceeds(tmp_path: Path) -> None:
    adapter = DeployFakeAdapter()
    adapter._schema_versions.append(("__deploy", "2026.08.14.01", "deploy"))
    stub = _StubCompare(_report([]))
    verdict = _analyze(tmp_path, adapter, stub)
    assert verdict.clean is True
    assert verdict.target_version == "2026.08.14.01"


# ------------------------------------------------------------------ gate logic


def test_changed_data_table_uncovered_violates(tmp_path: Path) -> None:
    adapter = DeployFakeAdapter()
    adapter.presence_stats = [_stats("app", "orders", 5000)]
    stub = _StubCompare(_report([_changed_entry()]))
    verdict = _analyze(tmp_path, adapter, stub)
    assert verdict.clean is False
    assert len(verdict.violations) == 1
    assert verdict.violations[0].object_schema == "app"
    assert verdict.violations[0].name == "orders"
    assert verdict.violations[0].estimated_rows == 5000


def test_removed_table_with_data_violates(tmp_path: Path) -> None:
    adapter = DeployFakeAdapter()
    adapter.presence_stats = [_stats("legacy", "logs", 10)]
    entry = DiffEntry(
        object_key="k", status=DiffStatus.REMOVED,
        source_snapshot=None, target_snapshot=_snap("table", "legacy", "logs"),
    )
    verdict = _analyze(tmp_path, adapter, _StubCompare(_report([entry])))
    assert verdict.clean is False
    assert verdict.violations[0].touch.value == "removed"


def test_covered_table_is_clean(tmp_path: Path) -> None:
    adapter = DeployFakeAdapter()
    adapter.presence_stats = [_stats("app", "orders", 5000)]
    stub = _StubCompare(_report([_changed_entry()]))
    codebase = _codebase(tmp_path)
    pre = codebase / "__migrations" / "pre"
    pre.mkdir(parents=True)
    (pre / "2026-08-14_001_migrate.sql").write_text(
        "/*====\n[<[autodoc-yaml]]\nobject:\n  object_type: pre_script\n"
        "  object_name: m\nproject:\n  build: true\n  covers:\n"
        "    - app.orders\n[[autodoc-yaml]>]\n====*/\nSELECT 1;\n",
        encoding="utf-8",
    )
    service = SafetyGateService(
        adapter_factory=lambda _c: adapter, compare_service=stub,
    )
    verdict = service.analyze(codebase, _cfg(), tmp_path / "report")
    assert verdict.clean is True
    assert verdict.touched[0].covered_by == ["2026-08-14_001_migrate.sql"]


def test_empty_table_changed_is_clean(tmp_path: Path) -> None:
    adapter = DeployFakeAdapter()
    adapter.presence_stats = [_stats("app", "orders", 0)]
    verdict = _analyze(tmp_path, adapter, _StubCompare(_report([_changed_entry()])))
    assert verdict.clean is True


def test_stale_stats_fail_safe(tmp_path: Path) -> None:
    adapter = DeployFakeAdapter()
    adapter.presence_stats = [_stats("app", "orders", 0, StatsConfidence.STALE)]
    verdict = _analyze(tmp_path, adapter, _StubCompare(_report([_changed_entry()])))
    assert verdict.clean is False  # stale 0 → UNKNOWN → treated as has-data


def test_missing_stats_fail_safe(tmp_path: Path) -> None:
    adapter = DeployFakeAdapter()  # no presence_stats → empty lookup
    verdict = _analyze(tmp_path, adapter, _StubCompare(_report([_changed_entry()])))
    assert verdict.clean is False
    assert verdict.touched[0].confidence is StatsConfidence.UNKNOWN


def test_added_and_unchanged_ignored(tmp_path: Path) -> None:
    added = DiffEntry(object_key="k1", status=DiffStatus.ADDED,
                      source_snapshot=_snap("table", "app", "brand_new"))
    unchanged = DiffEntry(
        object_key="k2", status=DiffStatus.UNCHANGED,
        source_snapshot=_snap("table", "app", "stable"),
        target_snapshot=_snap("table", "app", "stable"),
    )
    adapter = DeployFakeAdapter()
    adapter.presence_stats = [_stats("app", "stable", 999)]
    verdict = _analyze(tmp_path, adapter, _StubCompare(_report([added, unchanged])))
    assert verdict.touched == []
    assert verdict.clean is True


def test_non_table_objects_ignored(tmp_path: Path) -> None:
    func_changed = DiffEntry(
        object_key="k", status=DiffStatus.CHANGED,
        source_snapshot=_snap("function", "app", "sp_calc"),
        target_snapshot=_snap("function", "app", "sp_calc", sql_hash="cc"),
    )
    verdict = _analyze(tmp_path, DeployFakeAdapter(), _StubCompare(_report([func_changed])))
    assert verdict.touched == []


def test_service_schema_excluded(tmp_path: Path) -> None:
    adapter = DeployFakeAdapter()
    adapter.presence_stats = [
        _stats("__deploy", "schema_version", 7),
        _stats("app", "orders", 1),
    ]
    entry = DiffEntry(
        object_key="k", status=DiffStatus.REMOVED,
        target_snapshot=_snap("table", "__deploy", "schema_version"),
    )
    verdict = _analyze(tmp_path, adapter, _StubCompare(_report([entry])))
    assert verdict.touched == []  # __deploy touched → ignored (service schema)


# ------------------------------------------------------------------ contracts


def test_read_only_no_mutating_adapter_calls(tmp_path: Path) -> None:
    adapter = DeployFakeAdapter()
    adapter.presence_stats = [_stats("app", "orders", 10)]
    _analyze(tmp_path, adapter, _StubCompare(_report([_changed_entry()])))
    assert adapter.created_dbs == []
    assert adapter.dropped_dbs == []
    assert adapter.executed == []
    assert adapter._schema_versions == []
    assert adapter._script_history == {}
    assert adapter._script_audit == []


def test_reports_written(tmp_path: Path) -> None:
    adapter = DeployFakeAdapter()
    adapter.presence_stats = [_stats("app", "orders", 10)]
    out = tmp_path / "report"
    _analyze(tmp_path, adapter, _StubCompare(_report([_changed_entry()])))
    assert (out / "safety_gate_report.md").is_file()
    payload = json.loads((out / "safety_gate_report.json").read_text(encoding="utf-8"))
    assert payload["clean"] is False
    assert payload["db_type"] == "postgres"
    assert (out / DIFF_REPORT_FILENAME).is_file()  # companion artifact (SG-2)

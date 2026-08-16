"""Tests for db_project_manager.application.deploy_apply_service (Phase 12, S6).

Fake-adapter coverage of the apply pipeline: gate rejection before pre, CD-11
residual-unsafe rejection, execution order (pre → delta → post → record version),
stop-on-error without version recording, rehearsal isolation (target untouched on
rehearsal failure, seed only in rehearsal), --no-rehearsal, and the dry-run plan().
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from db_project_manager.application.deploy_apply_service import (
    ApplyResult,
    DeployApplyError,
    DeployApplyRejected,
    DeployApplyService,
)
from db_project_manager.domain.connection import ConnectionConfig
from db_project_manager.domain.deploy import ScriptRecord
from db_project_manager.domain.safety import SafetyGateVerdict, TablePresenceStats
from db_project_manager.infrastructure.database.base import DatabaseAdapter, DatabaseError

SERVICE_SCHEMA = "__deploy"
SOURCE_VERSION = "2026.08.16.01"
REHEARSAL_DB = "dbpm_rehearsal_20260816T120000"


# --------------------------------------------------------------------- fakes


class FakeApplyAdapter(DatabaseAdapter):
    """Full-contract in-memory adapter (LESSONS §45) sharing one call log."""

    def __init__(self, calls: list[tuple[str, str, Any]],
                 fail_scripts_containing: list[str]) -> None:
        self.calls = calls
        self.fail_scripts_containing = fail_scripts_containing
        self.database: str | None = None
        self.versions: list[tuple[str, str, str]] = []
        self.script_history: dict[tuple[str, str], ScriptRecord] = {}

    # --- connection
    def connect(self, cfg: ConnectionConfig) -> None:
        self.database = cfg.database

    def disconnect(self) -> None:
        pass

    # --- unused surfaces (contract no-ops)
    def get_database_structure(self) -> dict[str, Any]:
        return {"schemas": []}

    def check_can_create_db(self) -> bool:
        return True

    def get_server_timestamp_utc(self) -> str:
        return "20260816T120000"

    def create_database(self, name, *, encoding=None, lc_collate=None,
                        lc_ctype=None, template=None) -> None:
        self.calls.append(("create_database", self.database or "", name))

    def drop_database(self, name: str) -> None:
        self.calls.append(("drop_database", self.database or "", name))

    def get_table_row_counts(self) -> list[dict[str, Any]]:
        return []

    def get_table_presence_stats(self) -> list[TablePresenceStats]:
        return []   # empty lookup → UNKNOWN presence → fail-safe (safe ops still safe)

    def get_schema_version(self, schema_name: str) -> str | None:
        rows = [v for v in self.versions if v[0] == schema_name]
        return rows[-1][1] if rows else None

    def record_schema_version(self, schema_name: str, version: str, source: str) -> None:
        self.versions.append((schema_name, version, source))
        self.calls.append(("record_schema_version", self.database or "", (version, source)))

    def get_script_history(self, schema_name, script_name, script_type):
        return self.script_history.get((script_name, script_type))

    def record_script_execution(self, schema_name, record, deploy_version,
                                deploy_source) -> None:
        self.script_history[(record.script_name, record.script_type)] = record
        self.calls.append(("record_script", self.database or "", record.script_name))

    # --- the interesting part
    def execute_script(self, script: str) -> None:
        for token in self.fail_scripts_containing:
            if token in script:
                raise DatabaseError(f"simulated failure on {token!r}")
        self.calls.append(("execute_script", self.database or "", script))


class NewerVersionAdapter(FakeApplyAdapter):
    """Target already carries a newer calver version (forward-only check)."""

    def get_schema_version(self, schema_name: str) -> str | None:
        return "2027.01.01.01"


class AdapterFactory:
    """Creates fakes sharing one log; can fail scripts containing given tokens."""

    def __init__(self, adapter_cls: type[FakeApplyAdapter] = FakeApplyAdapter) -> None:
        self.adapter_cls = adapter_cls
        self.calls: list[tuple[str, str, Any]] = []
        self.adapters: list[FakeApplyAdapter] = []
        self.fail_scripts_containing: list[str] = []

    def __call__(self, cfg: ConnectionConfig) -> FakeApplyAdapter:
        adapter = self.adapter_cls(self.calls, self.fail_scripts_containing)
        self.adapters.append(adapter)
        return adapter

    @property
    def kinds(self) -> list[str]:
        return [c[0] for c in self.calls]


class StubGate:
    """Gate stub: records analyzed DBs; returns a verdict, optionally violating.

    When *report* is set, the stub also writes diff_report.json like the real
    gate does — that's what the residual-violation downgrade reads.
    """

    def __init__(self, *, clean: bool = True, report: dict | None = None) -> None:
        self.clean = clean
        self.report = report
        self.databases: list[str] = []

    def analyze(self, codebase_dir, target_cfg, output_dir, progress=None):
        from db_project_manager.application.compare_service import DIFF_REPORT_FILENAME
        from db_project_manager.domain.safety import (
            DataPresence,
            StatsConfidence,
            TableTouchKind,
            TouchedTable,
        )

        self.databases.append(target_cfg.database)
        if self.report is not None:
            out = Path(output_dir)
            out.mkdir(parents=True, exist_ok=True)
            (out / DIFF_REPORT_FILENAME).write_text(
                json.dumps(self.report, ensure_ascii=False), encoding="utf-8"
            )
        touched = [] if self.clean else [
            TouchedTable(
                object_schema="app", name="orders", touch=TableTouchKind.CHANGED,
                estimated_rows=100, confidence=StatsConfidence.FRESH,
                presence=DataPresence.HAS_DATA, covered_by=[],
            )
        ]
        return SafetyGateVerdict(
            clean=self.clean, db_type="postgres",
            source_version=SOURCE_VERSION, target_version=None, touched=touched,
        )


class StubCompare:
    """Compare stub writing a configurable diff_report.json."""

    def __init__(self, *, report: dict | None = None) -> None:
        self.report = report or _added_note_report()
        self.databases: list[str] = []

    def run(self, source, target, output_dir, *, keep_model_dir=False, progress=None):
        from db_project_manager.application.compare_service import DIFF_REPORT_FILENAME

        self.databases.append(target.conn_cfg.database)
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / DIFF_REPORT_FILENAME).write_text(
            json.dumps(self.report, ensure_ascii=False), encoding="utf-8"
        )
        return out


class StubReverseEngineer:
    """RE stub: 'writes' the target's own codebase (with its older version)."""

    def run(self, conn_cfg, output_root, progress=None):
        root = Path(output_root) / conn_cfg.database
        root.mkdir(parents=True, exist_ok=True)
        (root / "dbpm.manifest.json").write_text(
            '{"db_type": "postgres", "database": "target_db", '
            '"generated_at": "2026-08-16T00:00:00+00:00", "format_version": 2, '
            '"source_version": "2026.08.15.03"}',
            encoding="utf-8",
        )
        return root


class StubDeployValidate:
    """Rehearsal state-reproducer: 'creates' the rehearsal DB, deploys nothing."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.codebases: list[str] = []

    def run(self, conn_cfg, codebase_dir, *, prefix=None, keep_db=False,
            continue_on_error=False, progress=None):
        from db_project_manager.application.deploy_service import DeployResult

        self.codebases.append(str(codebase_dir))
        return DeployResult(success=not self.fail, db_name=REHEARSAL_DB)


# ------------------------------------------------------------------ fixtures

_ORDERS_KEY = "pg_database/target_db/schema/app/type/table/name/orders"


def _added_note_report(kind: str = "added") -> dict:
    return {
        "source": {"source_kind": "dir", "source_ref": "src", "db_type": "postgres",
                   "generated_at": "2026-08-16T00:00:00+00:00", "objects": {}, "edges": []},
        "target": {"source_kind": "db", "source_ref": "target", "db_type": "postgres",
                   "generated_at": "2026-08-16T00:00:00+00:00", "objects": {}, "edges": []},
        "generated_at": "2026-08-16T00:00:00+00:00",
        "summary": {"changed": 1},
        "entries": [{
            "object_key": _ORDERS_KEY, "status": "changed",
            "source_snapshot": {
                "object_key": _ORDERS_KEY, "object_schema": "app",
                "object_name": "orders", "object_type": "table",
                "sql_normalized": "x", "sql_hash": "h1",
                "columns": [
                    {"name": "id", "type": "int", "nullable": False},
                    {"name": "note", "type": "text", "nullable": True},
                ],
            },
            "target_snapshot": {
                "object_key": _ORDERS_KEY, "object_schema": "app",
                "object_name": "orders", "object_type": "table",
                "sql_normalized": "x", "sql_hash": "h2",
                "columns": [{"name": "id", "type": "int", "nullable": False}],
            },
            "column_diffs": [{
                "column": "note", "kind": kind,
                "source_column": {"name": "note", "type": "text", "nullable": True},
                "target_column": {"name": "note", "type": "text", "nullable": True},
            }],
            "columns_unavailable": False,
        }],
        "edge_summary": {}, "edge_entries": [],
    }


def _conn(database: str = "target_db") -> ConnectionConfig:
    return ConnectionConfig(
        name="target", type="postgres", host="localhost", port=5432,
        database=database, username="u", password="p",
    )


def _make_codebase(tmp_path: Path, *, pre: str | None = None,
                   seed: str | None = None) -> Path:
    from db_project_manager.infrastructure.sql.autodoc import build_metadata, render_header

    root = tmp_path / "codebase"
    meta = build_metadata(
        object_catalog="target_db", object_schema="app",
        object_type="table", object_name="orders",
    )
    (root / "app" / "tables").mkdir(parents=True)
    (root / "app" / "tables" / "table orders.sql").write_text(
        render_header(meta) + "CREATE TABLE app.orders (id int NOT NULL, note text);\n",
        encoding="utf-8", newline="\n",
    )
    (root / "dbpm.manifest.json").write_text(
        '{"db_type": "postgres", "database": "target_db", '
        '"generated_at": "2026-08-16T00:00:00+00:00", "format_version": 2, '
        f'"source_version": "{SOURCE_VERSION}"}}',
        encoding="utf-8", newline="\n",
    )
    if pre is not None:
        predir = root / "__migrations" / "pre"
        predir.mkdir(parents=True)
        (predir / "2026-08-16_001_pre.sql").write_text(pre, encoding="utf-8", newline="\n")
    if seed is not None:
        seeddir = root / "__migrations" / "seed"
        seeddir.mkdir(parents=True)
        (seeddir / "2026-08-16_001_seed.sql").write_text(seed, encoding="utf-8", newline="\n")
    return root


def _service(factory: AdapterFactory, *, gate: StubGate | None = None,
             compare: StubCompare | None = None,
             deploy_validate: StubDeployValidate | None = None) -> DeployApplyService:
    service = DeployApplyService(
        adapter_factory=factory,
        compare_service=compare or StubCompare(),
        deploy_validate=deploy_validate or StubDeployValidate(),
        reverse_engineer=StubReverseEngineer(),
        service_schema=SERVICE_SCHEMA,
    )
    service._gate = gate or StubGate()  # noqa: SLF001 — test injection point
    return service


# -------------------------------------------------------------------- tests


def test_plan_writes_artifacts_and_mutates_nothing(tmp_path: Path) -> None:
    codebase = _make_codebase(tmp_path)
    factory = AdapterFactory()
    service = _service(factory)
    out = tmp_path / "out"
    plan = service.plan(codebase, _conn(), out)
    assert (out / "delta").is_dir()
    assert (out / "plan.json").exists()
    assert plan.db_type == "postgres"
    assert "execute_script" not in factory.kinds
    assert "record_script" not in factory.kinds
    assert service._gate.databases == ["target_db"]  # noqa: SLF001


def test_apply_success_order_pre_delta_post_version(tmp_path: Path) -> None:
    codebase = _make_codebase(
        tmp_path,
        pre="ALTER TABLE app.orders ADD COLUMN IF NOT EXISTS note text;\n",
    )
    factory = AdapterFactory()
    service = _service(factory)
    out = tmp_path / "out"
    result = service.apply(codebase, _conn(), out, rehearsal=False)
    assert isinstance(result, ApplyResult)
    assert result.applied_version == SOURCE_VERSION
    assert result.rehearsal_db is None

    kinds = factory.kinds
    assert "record_script" in kinds                       # pre ran
    assert "execute_script" in kinds                      # delta applied
    assert factory.calls[-1] == (
        "record_schema_version", "target_db", (SOURCE_VERSION, "apply")
    )
    delta_execs = [c for c in factory.calls if c[0] == "execute_script"]
    assert any('ALTER TABLE "app"."orders"' in s for _, _, s in delta_execs)
    assert (out / "plan.json").exists()


def test_gate_violation_stops_before_pre(tmp_path: Path) -> None:
    """Violating gate without a readable diff report → conservative rejection."""
    codebase = _make_codebase(tmp_path, pre="SELECT 1;\n", seed="SELECT 1;\n")
    factory = AdapterFactory()
    service = _service(factory, gate=StubGate(clean=False))
    with pytest.raises(DeployApplyRejected, match="safety-gate"):
        service.apply(codebase, _conn(), tmp_path / "out", rehearsal=False)
    assert "record_script" not in factory.kinds
    assert "execute_script" not in factory.kinds


def test_gate_violation_downgraded_for_safe_delta(tmp_path: Path) -> None:
    """Gate flags the data table, but its diff classifies SAFE (ALT-3) → proceed."""
    codebase = _make_codebase(tmp_path)
    factory = AdapterFactory()
    gate = StubGate(clean=False, report=_added_note_report(kind="added"))
    service = _service(factory, gate=gate)
    result = service.apply(codebase, _conn(), tmp_path / "out", rehearsal=False)
    assert result.applied >= 1  # the safe alter went through
    delta_execs = [
        c for c in factory.calls
        if c[0] == "execute_script" and 'ALTER TABLE "app"."orders"' in c[2]
    ]
    assert delta_execs


def test_gate_violation_kept_for_unsafe_delta(tmp_path: Path) -> None:
    """Gate flags the table AND its diff is a drop → rejection stands."""
    codebase = _make_codebase(tmp_path)
    factory = AdapterFactory()
    gate = StubGate(clean=False, report=_added_note_report(kind="dropped"))
    service = _service(factory, gate=gate)
    with pytest.raises(DeployApplyRejected, match="safety-gate"):
        service.apply(codebase, _conn(), tmp_path / "out", rehearsal=False)
    delta_execs = [
        c for c in factory.calls
        if c[0] == "execute_script" and "ALTER TABLE" in c[2]
    ]
    assert delta_execs == []


def test_cd11_residual_unsafe_rejects(tmp_path: Path) -> None:
    compare = StubCompare(report=_added_note_report(kind="dropped"))
    factory = AdapterFactory()
    service = _service(factory, compare=compare)
    codebase = _make_codebase(tmp_path, pre="SELECT 1;\n")
    with pytest.raises(DeployApplyRejected, match="CD-11"):
        service.apply(codebase, _conn(), tmp_path / "out", rehearsal=False)
    # pre-script execution is legitimate; the DELTA must not have been applied
    delta_execs = [
        c for c in factory.calls
        if c[0] == "execute_script" and 'ALTER TABLE' in c[2]
    ]
    assert delta_execs == []


def test_error_midway_no_version_recorded(tmp_path: Path) -> None:
    codebase = _make_codebase(tmp_path)
    factory = AdapterFactory()
    factory.fail_scripts_containing = ['ADD COLUMN "note"']
    service = _service(factory)
    with pytest.raises(DeployApplyError, match="stop-on-error"):
        service.apply(codebase, _conn(), tmp_path / "out", rehearsal=False)
    assert all(a.versions == [] for a in factory.adapters)


def test_rehearsal_failure_leaves_target_untouched(tmp_path: Path) -> None:
    codebase = _make_codebase(tmp_path)
    factory = AdapterFactory()
    factory.fail_scripts_containing = ['ADD COLUMN "note"']  # fails on rehearsal apply
    service = _service(factory)
    with pytest.raises(DeployApplyError, match="Репетиция"):
        service.apply(codebase, _conn(), tmp_path / "out")
    # the target pipeline never ran: the gate only saw the rehearsal DB
    assert service._gate.databases == [REHEARSAL_DB]  # noqa: SLF001
    drops = [c for c in factory.calls if c[0] == "drop_database"]
    assert drops == [("drop_database", "target_db", REHEARSAL_DB)]


def test_seed_runs_only_against_rehearsal(tmp_path: Path) -> None:
    codebase = _make_codebase(tmp_path, seed="INSERT INTO app.orders DEFAULT VALUES;\n")
    factory = AdapterFactory()
    service = _service(factory)
    result = service.apply(codebase, _conn(), tmp_path / "out")
    seed_execs = [
        c for c in factory.calls
        if c[0] == "execute_script" and "INSERT INTO" in c[2]
    ]
    assert len(seed_execs) == 1
    assert seed_execs[0][1] == REHEARSAL_DB
    assert result.rehearsal_db == REHEARSAL_DB
    assert (tmp_path / "out" / "rehearsal" / "plan.json").exists()


def test_rehearsal_db_kept_with_flag(tmp_path: Path) -> None:
    codebase = _make_codebase(tmp_path)
    factory = AdapterFactory()
    service = _service(factory)
    service.apply(codebase, _conn(), tmp_path / "out", keep_rehearsal_db=True)
    assert "drop_database" not in factory.kinds


def test_no_rehearsal_flag_skips_phase_a(tmp_path: Path) -> None:
    codebase = _make_codebase(tmp_path)
    factory = AdapterFactory()
    stub_validate = StubDeployValidate()
    service = _service(factory, deploy_validate=stub_validate)
    result = service.apply(codebase, _conn(), tmp_path / "out", rehearsal=False)
    assert stub_validate.codebases == []
    assert result.rehearsal_db is None


def test_version_newer_hard_error(tmp_path: Path) -> None:
    codebase = _make_codebase(tmp_path)
    factory = AdapterFactory(adapter_cls=NewerVersionAdapter)
    service = _service(factory)
    with pytest.raises(DeployApplyError, match="Forward-only"):
        service.plan(codebase, _conn(), tmp_path / "out")

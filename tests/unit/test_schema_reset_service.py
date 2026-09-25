"""Unit tests for ``db_project_manager.application.schema_reset_service`` (Phase 18).

Contract under test (final doc 20260917_001 §6):

* connection-flag gate fires BEFORE connecting (rejected fast) and re-checks
  in execute (defense in depth);
* manifest/db_type fail-fast mirrors SG-M;
* differentiated mechanics (D9): codebase schemas + public → content-drop,
  junk schemas (absent from the codebase) → full DROP SCHEMA;
* the service schema never appears among victims;
* extensions residing in wiped schemas are dropped first;
* journal tables are truncated; a missing __deploy degrades to a warning;
* dry-run mutates nothing but still writes the ACL snapshot + reports;
* the ACL insurance snapshot is on disk before any mutation happens.

All against a recording fake adapter — the multi-DB contract (D11): the
service must be testable with nothing but the DatabaseAdapter ABC.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from db_project_manager.application.schema_reset_service import (
    SchemaResetError,
    SchemaResetRejected,
    SchemaResetService,
)
from db_project_manager.domain.connection import ConnectionConfig
from db_project_manager.domain.deploy import ScriptRecord
from db_project_manager.domain.safety import TablePresenceStats
from db_project_manager.infrastructure.database.base import DatabaseAdapter, DatabaseError

ACL_SQL = "-- canned acl snapshot\n"


class FakeResetAdapter(DatabaseAdapter):
    """Full-ABC adapter recording every reset-relevant call (D11 contract)."""

    def __init__(
        self,
        schemas: list[str] | None = None,
        counts: dict[str, int] | None = None,
        extensions: list[dict] | None = None,
        *,
        fail_on: str = "",
    ) -> None:
        self.schemas = schemas if schemas is not None else ["public", "cis_app", "junk_old"]
        self.counts = counts or {"public": 5, "cis_app": 40, "junk_old": 7}
        self.extensions = extensions or [
            {"name": "uuid-ossp", "schema": "public", "version": "1.1", "comment": None},
            {"name": "pg_trgm", "schema": "pg_catalog", "version": "1.4", "comment": None},
        ]
        self.fail_on = fail_on  # token raising DatabaseError when a call name contains it
        self.calls: list[str] = []
        self.connected = 0

    def _record(self, name: str) -> None:
        if self.fail_on and self.fail_on in name:
            raise DatabaseError(f"simulated failure on {name}")
        self.calls.append(name)

    # --- connection / unused surfaces
    def connect(self, cfg: ConnectionConfig) -> None:
        self.connected += 1

    def disconnect(self) -> None:
        pass

    def get_database_structure(self) -> dict[str, Any]:
        return {"schemas": []}

    def check_can_create_db(self) -> bool:
        return True

    def get_server_timestamp_utc(self) -> str:
        return "20260917T000000"

    def create_database(self, name, *, encoding=None, lc_collate=None,
                        lc_ctype=None, template=None) -> None:
        pass

    def drop_database(self, name: str) -> None:
        pass

    def execute_script(self, script: str) -> None:
        pass

    def get_table_row_counts(self) -> list[dict[str, Any]]:
        return []

    def get_table_presence_stats(self) -> list[TablePresenceStats]:
        return []

    def get_schema_version(self, schema_name: str) -> str | None:
        return None

    def record_schema_version(self, schema_name, version, source) -> None:
        pass

    def get_script_history(self, schema_name, script_name, script_type):
        return None

    def record_script_execution(self, schema_name, record: ScriptRecord,
                                deploy_version, deploy_source) -> None:
        pass

    # --- Phase 18 reset surface (the interesting part)
    def list_schemas(self) -> list[str]:
        return list(self.schemas)

    def get_schema_object_counts(self) -> dict[str, int]:
        return dict(self.counts)

    def drop_schema(self, name: str) -> None:
        self._record(f"drop_schema:{name}")

    def drop_schema_contents(self, schema: str) -> None:
        self._record(f"drop_schema_contents:{schema}")

    def snapshot_schema_acls(self, schemas: list[str]) -> str:
        return ACL_SQL

    def truncate_table(self, schema: str, name: str) -> None:
        self._record(f"truncate:{schema}.{name}")

    def drop_extension(self, name: str) -> None:
        self._record(f"drop_extension:{name}")

    def list_extensions(self) -> list[dict[str, Any]]:
        return list(self.extensions)


class AdapterFactory:
    """Creates one shared fake per config so connect counting is accurate."""

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.adapters: list[FakeResetAdapter] = []

    def __call__(self, cfg: ConnectionConfig) -> FakeResetAdapter:
        adapter = FakeResetAdapter(**self.kwargs)
        self.adapters.append(adapter)
        return adapter

    @property
    def adapter(self) -> FakeResetAdapter:
        assert self.adapters, "no adapter created"
        return self.adapters[-1]


def _codebase(tmp_path: Path) -> Path:
    root = tmp_path / "codebase"
    (root / "cis_app" / "tables").mkdir(parents=True)
    (root / "cis_app" / "tables" / "t.sql").write_text("-- x", encoding="utf-8")
    (root / "__migrations").mkdir()
    (root / "settings").mkdir()
    # Tool-owned hidden dir (graph store) — must never count as a schema
    # (leaked into in_codebase on the first live run, 2026-09-17).
    (root / ".dbm_graph").mkdir()
    (root / "dbpm.manifest.json").write_text(
        json.dumps({
            "db_type": "postgres",
            "database": "dev",
            "generated_at": "2026-09-17T00:00:00+00:00",
            "tool_version": "0.1.0",
            "format_version": 2,
            "source_version": "2026.09.17.01",
        }),
        encoding="utf-8",
    )
    return root


def _cfg(*, allow: bool = True, db_type: str = "postgres") -> ConnectionConfig:
    return ConnectionConfig(
        name="dev", host="localhost", port=5432, database="devdb",
        username="u", password="p", type=db_type, allow_drop_schemas=allow,
    )


# --- flag gate (D3/D6) ---


def test_collect_without_flag_rejects_before_connect(tmp_path: Path) -> None:
    factory = AdapterFactory()
    service = SchemaResetService(adapter_factory=factory)
    with pytest.raises(SchemaResetRejected, match="allow_drop_schemas"):
        service.collect(_codebase(tmp_path), _cfg(allow=False))
    assert not factory.adapters  # no connection was ever opened


def test_execute_rechecks_flag_defense_in_depth(tmp_path: Path) -> None:
    factory = AdapterFactory()
    service = SchemaResetService(adapter_factory=factory)
    plan = service.collect(_codebase(tmp_path), _cfg(allow=True))
    plan.target = plan.target.model_copy(update={"allow_drop_schemas": False})
    with pytest.raises(SchemaResetRejected):
        service.execute(plan, tmp_path / "out")


# --- fail-fast checks (SG-M pattern) ---


def test_db_type_mismatch_is_hard_error(tmp_path: Path) -> None:
    service = SchemaResetService(adapter_factory=AdapterFactory())
    with pytest.raises(SchemaResetError, match="не совпадает"):
        service.collect(_codebase(tmp_path), _cfg(db_type="greenplum"))


def test_missing_manifest_is_hard_error(tmp_path: Path) -> None:
    service = SchemaResetService(adapter_factory=AdapterFactory())
    with pytest.raises(SchemaResetError, match="manifest"):
        service.collect(tmp_path / "empty", _cfg())


# --- differentiated mechanics (D9) ---


def test_content_drop_vs_full_drop_and_extensions(tmp_path: Path) -> None:
    factory = AdapterFactory()
    service = SchemaResetService(adapter_factory=factory)
    plan = service.collect(_codebase(tmp_path), _cfg())
    assert plan.in_codebase == {"cis_app"}
    assert ".dbm_graph" not in plan.in_codebase  # hidden tool dirs are not schemas
    assert plan.service_schema not in plan.schemas
    assert plan.is_content_drop("public")       # always content-drop
    assert plan.is_content_drop("cis_app")      # in codebase
    assert not plan.is_content_drop("junk_old")  # junk → full DROP

    result = service.execute(plan, tmp_path / "out")

    calls = factory.adapter.calls
    # Extensions in wiped schemas go first, then content-drops, then full drops.
    assert calls.index("drop_extension:uuid-ossp") == 0
    assert "drop_schema_contents:public" in calls
    assert "drop_schema_contents:cis_app" in calls
    assert "drop_schema:junk_old" in calls
    # pg_trgm lives in pg_catalog — not a victim schema, never touched.
    assert not any("pg_trgm" in c for c in calls)
    # Journal tables truncated (D2).
    assert "truncate:__deploy.script_history" in calls
    assert "truncate:__deploy.script_audit_log" in calls

    assert result.schemas_wiped == ["public", "cis_app"]
    assert result.schemas_dropped == ["junk_old"]
    assert result.extensions_dropped == ["uuid-ossp"]
    assert result.journal_truncated is True


def test_acl_snapshot_written_before_mutations(tmp_path: Path) -> None:
    factory = AdapterFactory()
    service = SchemaResetService(adapter_factory=factory)
    plan = service.collect(_codebase(tmp_path), _cfg())
    out = tmp_path / "out"
    service.execute(plan, out)
    snapshot = out / "reset_acl_snapshot.sql"
    assert snapshot.read_text(encoding="utf-8") == ACL_SQL


def test_dry_run_mutates_nothing_but_writes_artifacts(tmp_path: Path) -> None:
    factory = AdapterFactory()
    service = SchemaResetService(adapter_factory=factory)
    plan = service.collect(_codebase(tmp_path), _cfg())
    out = tmp_path / "out"
    result = service.execute(plan, out, dry_run=True)

    # collect connected once; dry-run execute never connects again
    assert factory.adapters[0].connected == 1
    assert factory.adapter.calls == []
    assert result.dry_run is True
    assert result.schemas_wiped == [] and result.schemas_dropped == []
    assert (out / "reset_acl_snapshot.sql").exists()
    assert (out / "reset_report.json").exists()
    assert (out / "reset_report.md").read_text(encoding="utf-8").startswith("# deploy reset")


def test_journal_truncate_failure_is_not_fatal(tmp_path: Path) -> None:
    factory = AdapterFactory(fail_on="truncate:")
    service = SchemaResetService(adapter_factory=factory)
    plan = service.collect(_codebase(tmp_path), _cfg())
    result = service.execute(plan, tmp_path / "out")  # must not raise
    assert result.journal_truncated is False
    assert result.schemas_wiped == ["public", "cis_app"]


def test_stop_on_error_on_first_ddl_failure(tmp_path: Path) -> None:
    factory = AdapterFactory(fail_on="drop_extension:")
    service = SchemaResetService(adapter_factory=factory)
    plan = service.collect(_codebase(tmp_path), _cfg())
    with pytest.raises(SchemaResetError, match="uuid-ossp"):
        service.execute(plan, tmp_path / "out")
    # Nothing was dropped after the extension failure (stop-on-error).
    assert factory.adapter.calls == []

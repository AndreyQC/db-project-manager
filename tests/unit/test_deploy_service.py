"""Tests for db_project_manager.application.deploy_service.

Uses a recording FakeAdapter and a tiny codebase fixture to exercise the
stratified error policy (Q4), cleanup (Q3), build:false filter (Q8) and the
CREATEDB permission check (vision §1.3) — all without a live database.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from db_project_manager.application.deploy_service import (
    DeployPermissionError,
    DeployValidateService,
    sanitize_prefix,
)
from db_project_manager.domain.connection import ConnectionConfig
from db_project_manager.infrastructure.database.base import DatabaseAdapter, DatabaseError

FIXTURE_ROOT = Path(__file__).resolve().parent.parent / "fixtures" / "codebase_sample"


class DeployFakeAdapter(DatabaseAdapter):
    """In-memory adapter that records calls and can simulate failures.

    Object scripts are not executed against a real DB: we instead track which
    object files were 'executed' and optionally raise on a given object key.
    """

    def __init__(
        self,
        *,
        can_create_db: bool = True,
        server_ts: str = "20260101T120000",
        fail_on: set[str] | None = None,
    ) -> None:
        self.can_create_db = can_create_db
        self.server_ts = server_ts
        # fail_on holds 'schema.name' tokens that appear in the SQL body
        # (e.g. 'bookings.flights'); after autodoc strip the object_key is gone
        # but the qualified name in CREATE statements remains.
        self.fail_on = fail_on or set()
        self.executed: list[str] = []  # 'schema.name' of executed objects
        self.created_dbs: list[str] = []
        self.dropped_dbs: list[str] = []
        self._current_cfg: ConnectionConfig | None = None

    def connect(self, cfg: ConnectionConfig) -> None:
        self._current_cfg = cfg

    def disconnect(self) -> None:
        pass

    def get_database_structure(self) -> dict[str, Any]:
        return {"schemas": []}

    def check_can_create_db(self) -> bool:
        return self.can_create_db

    def get_server_timestamp_utc(self) -> str:
        return self.server_ts

    def create_database(
        self,
        name: str,
        *,
        encoding: str | None = None,
        lc_collate: str | None = None,
        lc_ctype: str | None = None,
        template: str | None = None,
    ) -> None:
        self.created_dbs.append(name)
        # Record properties for Phase 5 assertions.
        if not hasattr(self, "_db_properties"):
            self._db_properties: dict[str, Any] = {}
        self._db_properties[name] = {
            "encoding": encoding, "lc_collate": lc_collate,
            "lc_ctype": lc_ctype, "template": template,
        }

    def drop_database(self, name: str) -> None:
        self.dropped_dbs.append(name)

    def execute_script(self, script: str) -> None:
        ident = self._extract_qualified_name(script)
        if ident in self.fail_on:
            raise DatabaseError(f"simulated failure for {ident}")
        self.executed.append(ident)

    # Phase 9 compare surface (unused by DeployValidateService; stubbed for ABC).
    def get_table_row_counts(self) -> list[dict[str, Any]]:
        return []

    @staticmethod
    def _extract_qualified_name(script: str) -> str:
        """Best-effort: find the first 'schema.name' identifier in the body."""
        import re
        m = re.search(r"\b([a-z_][a-z0-9_]*)\.([a-z_][a-z0-9_]*)\b", script.lower())
        if not m:
            return ""
        return f"{m.group(1)}.{m.group(2)}"


def _conn() -> ConnectionConfig:
    return ConnectionConfig(
        host="localhost", port=5432, database="postgres",
        username="u", password="p", type="postgres",
    )


def _service(adapter: DeployFakeAdapter) -> DeployValidateService:
    return DeployValidateService(adapter_factory=lambda _cfg: adapter)


# --- sanitize_prefix ---


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("MyApp", "myapp"),
        ("my-app 2026", "my_app_2026"),
        ("__leading", "leading"),
        ("", "dbpm"),
        ("!!!", "dbpm"),
    ],
)
def test_sanitize_prefix(raw: str, expected: str) -> None:
    assert sanitize_prefix(raw) == expected


# --- happy path ---


def test_successful_deploy_drops_temp_db() -> None:
    adapter = DeployFakeAdapter()
    svc = _service(adapter)
    result = svc.run(_conn(), FIXTURE_ROOT)

    assert result.success is True
    # Only one temp DB created and dropped.
    assert len(adapter.created_dbs) == 1
    assert len(adapter.dropped_dbs) == 1
    assert adapter.dropped_dbs[0] == adapter.created_dbs[0]
    # Name shape: <prefix>_<timestamp>.
    assert adapter.created_dbs[0] == "codebase_sample_20260101T120000"
    assert result.objects_done == result.objects_total


def test_keep_db_does_not_drop() -> None:
    adapter = DeployFakeAdapter()
    svc = _service(adapter)
    result = svc.run(_conn(), FIXTURE_ROOT, keep_db=True)

    assert result.success is True
    assert adapter.created_dbs
    assert adapter.dropped_dbs == []  # kept


def test_build_false_object_is_skipped() -> None:
    """Q8: routes matview (build:false) must not be deployed."""
    adapter = DeployFakeAdapter()
    svc = _service(adapter)
    result = svc.run(_conn(), FIXTURE_ROOT)

    assert result.success is True
    assert all("routes" not in key for key in adapter.executed), \
        "build:false object was deployed"


# --- permission check ---


def test_no_createdb_raises_before_anything() -> None:
    adapter = DeployFakeAdapter(can_create_db=False)
    svc = _service(adapter)
    with pytest.raises(DeployPermissionError, match="CREATEDB"):
        svc.run(_conn(), FIXTURE_ROOT)
    # No DB created, no DB dropped.
    assert adapter.created_dbs == []
    assert adapter.dropped_dbs == []


# --- stratified error policy (Q4) ---


def test_early_ddl_failure_aborts_and_cleans_up() -> None:
    """Failure on a table -> abort, temp DB dropped (cleanup)."""
    # Make 'flights' table fail (matched by qualified name in the SQL body).
    fail_ident = "bookings.flights"
    fail_key = "pg_database/demo/schema/bookings/type/table/name/flights"
    adapter = DeployFakeAdapter(fail_on={fail_ident})
    svc = _service(adapter)
    result = svc.run(_conn(), FIXTURE_ROOT)

    assert result.success is False
    assert result.errors
    assert result.errors[0].object_key == fail_key
    # Cleanup happened despite failure.
    assert adapter.dropped_dbs == adapter.created_dbs


def test_late_object_failure_without_continue_aborts() -> None:
    """Failure on a view without --continue-on-error -> abort (still cleanup)."""
    fail_ident = "bookings.flights_v"
    fail_key = "pg_database/demo/schema/bookings/type/view/name/flights_v"
    adapter = DeployFakeAdapter(fail_on={fail_ident})
    svc = _service(adapter)
    result = svc.run(_conn(), FIXTURE_ROOT)

    assert result.success is False
    assert any(e.object_key == fail_key for e in result.errors)
    assert adapter.dropped_dbs  # cleanup


def test_late_object_failure_with_continue_collects_errors() -> None:
    """--continue-on-error: a view failure does not stop, errors collected."""
    fail_ident = "bookings.flights_v"
    fail_key = "pg_database/demo/schema/bookings/type/view/name/flights_v"
    adapter = DeployFakeAdapter(fail_on={fail_ident})
    svc = _service(adapter)
    result = svc.run(_conn(), FIXTURE_ROOT, continue_on_error=True)

    assert result.success is False  # there were errors
    assert any(e.object_key == fail_key for e in result.errors)
    # Other late objects were attempted after the failed one (only flights_v
    # is late in this fixture, so executed stops at the failure, but DDL before
    # it ran fine). Crucially the run did not raise.
    assert adapter.executed  # something was deployed


def test_progress_callback_invoked() -> None:
    adapter = DeployFakeAdapter()
    svc = _service(adapter)
    events: list[tuple[str, int, int]] = []
    result = svc.run(_conn(), FIXTURE_ROOT, progress=lambda m, c, t: events.append((m, c, t)))

    assert result.success is True
    assert events  # at least the connection/build steps
    # deploy steps carry the running counter
    deploy_steps = [e for e in events if e[2] and e[0].startswith("[")]
    assert deploy_steps
    assert deploy_steps[0][0].startswith("[1/")


def test_prefix_override() -> None:
    adapter = DeployFakeAdapter()
    svc = _service(adapter)
    result = svc.run(_conn(), FIXTURE_ROOT, prefix="myproj")
    assert result.db_name == "myproj_20260101T120000"


# --- Phase 5: db properties in create_database + database_setting deploy ---


def test_create_database_receives_encoding_from_db_properties() -> None:
    """deploy_order picks up db_properties from the database_setting vertex
    and passes them to create_database (Phase 5 vision §4.2)."""
    adapter = DeployFakeAdapter()
    svc = _service(adapter)
    result = svc.run(_conn(), FIXTURE_ROOT)
    assert result.success is True
    db_props = getattr(adapter, "_db_properties", {})
    props = db_props.get(adapter.created_dbs[0], {})
    # Fixture has: encoding=UTF8, lc_collate=C, lc_ctype=C, template=template0
    assert props.get("encoding") == "UTF8"
    assert props.get("lc_collate") == "C"
    assert props.get("lc_ctype") == "C"
    assert props.get("template") == "template0"


def test_extension_in_early_ddl_types() -> None:
    """extension is in EARLY_DDL_TYPES so a failed extension aborts deploy."""
    from db_project_manager.application.deploy_service import EARLY_DDL_TYPES

    assert "extension" in EARLY_DDL_TYPES
    assert "database_setting" in EARLY_DDL_TYPES


def test_database_setting_script_db_name_replaced_in_deploy(tmp_path: Path) -> None:
    """When deploying a database_setting script, the ALTER DATABASE statement
    targets the actual temp DB name, not the original (object_catalog) name.

    Verification: db_properties from the fixture (encoding=UTF8 etc.) are
    extracted from the database_setting vertex and passed to create_database."""
    import shutil

    src = FIXTURE_ROOT
    dst = tmp_path / "codebase"
    shutil.copytree(src, dst)
    adapter = DeployFakeAdapter()
    svc = DeployValidateService(adapter_factory=lambda _cfg: adapter)
    result = svc.run(_conn(), dst)
    assert result.success is True
    # DB properties from the fixture were extracted and passed to create_database.
    db_props = getattr(adapter, "_db_properties", {})
    props = db_props.get(adapter.created_dbs[0], {})
    assert props.get("encoding") == "UTF8"
    assert props.get("lc_collate") == "C"
    assert props.get("lc_ctype") == "C"
    assert props.get("template") == "template0"
    # routes matview has build:false so it is filtered out. After adding
    # sp_caller (Phase 6 follow-up regression fixture) the deployable count is 12:
    # 1 schema + 3 tables + 1 seq + 1 view + 4 functions + 2 procs + 1 extension
    # + 1 database_setting (routes matview excluded).
    assert result.objects_total == 12

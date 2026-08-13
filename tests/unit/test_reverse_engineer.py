"""Tests for db_project_manager.application.reverse_engineer."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from db_project_manager.application.reverse_engineer import (
    ReverseEngineerError,
    ReverseEngineerService,
)
from db_project_manager.domain.connection import ConnectionConfig
from db_project_manager.infrastructure.database.base import DatabaseAdapter, DatabaseError

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def _load_structure() -> dict[str, Any]:
    with (FIXTURES / "sample_structure.json").open("r", encoding="utf-8") as f:
        return json.load(f)


class FakeAdapter(DatabaseAdapter):
    """In-memory adapter returning a fixed structure."""

    def __init__(self, structure: dict[str, Any] | None = None, *, fail: bool = False) -> None:
        self._structure = structure if structure is not None else _load_structure()
        self._fail = fail
        self.connected = False
        self.disconnected = False

    def connect(self, cfg: ConnectionConfig) -> None:
        if self._fail:
            raise DatabaseError("connection refused")
        self.connected = True

    # Phase 2 deploy surface (unused by ReverseEngineerService; stubbed for ABC).
    def check_can_create_db(self) -> bool:
        return True

    def get_server_timestamp_utc(self) -> str:
        return "20260101T000000"

    def create_database(self, name: str) -> None:
        pass

    def drop_database(self, name: str) -> None:
        pass

    def execute_script(self, script: str) -> None:
        pass
    def disconnect(self) -> None:
        self.disconnected = True

    def get_database_structure(self) -> dict[str, Any]:
        return self._structure

    # Phase 9 compare surface (unused by ReverseEngineerService; stubbed for ABC).
    def get_table_row_counts(self) -> list[dict[str, Any]]:
        return []

    # Phase 10 CD Foundation surface (unused by reverse-engineer; stubbed for ABC).
    def get_schema_version(self, schema_name: str) -> str | None:  # noqa: ARG002
        return None

    def record_schema_version(self, schema_name: str, version: str, source: str) -> None:  # noqa: ARG002
        pass

    def get_script_history(self, schema_name: str, script_name: str, script_type: str):  # noqa: ARG002
        return None

    def record_script_execution(self, schema_name, record, deploy_version, deploy_source):  # noqa: ARG002
        pass


def _cfg() -> ConnectionConfig:
    return ConnectionConfig(
        host="localhost", port=5432, database="mydb", username="u", password="p", type="postgres"
    )


def test_service_runs_full_flow(tmp_path) -> None:
    adapter = FakeAdapter()
    service = ReverseEngineerService(adapter_factory=lambda _cfg: adapter)

    progress_log: list[tuple[str, int, int]] = []
    out = service.run(_cfg(), tmp_path, progress=lambda m, c, t: progress_log.append((m, c, t)))

    assert adapter.connected and adapter.disconnected
    # per-database subdir created
    assert (out / "bookings" / "tables" / "table aircrafts.sql").is_file()
    # progress emitted with growing counter, ends at total
    assert progress_log[0][2] == 4
    assert progress_log[-1] == ("Готово", 4, 4)
    assert out == tmp_path / "mydb"
    # Phase 9: a whole-DB manifest is written to the codebase root.
    from db_project_manager.infrastructure.config.codebase_manifest import read_manifest

    manifest = read_manifest(out)
    assert manifest.db_type == "postgres"
    assert manifest.database == "mydb"


def test_service_wraps_database_error(tmp_path) -> None:
    adapter = FakeAdapter(fail=True)
    service = ReverseEngineerService(adapter_factory=lambda _cfg: adapter)

    with pytest.raises(ReverseEngineerError, match="connection refused"):
        service.run(_cfg(), tmp_path)


def test_service_disconnects_on_error(tmp_path) -> None:
    adapter = FakeAdapter(fail=True)
    service = ReverseEngineerService(adapter_factory=lambda _cfg: adapter)

    with pytest.raises(ReverseEngineerError):
        service.run(_cfg(), tmp_path)
    # disconnect is still called in the finally block
    assert adapter.disconnected


def test_service_no_progress_callback(tmp_path) -> None:
    adapter = FakeAdapter()
    service = ReverseEngineerService(adapter_factory=lambda _cfg: adapter)
    # Must not raise when progress is None.
    out = service.run(_cfg(), tmp_path, progress=None)
    assert out.is_dir()


def test_qualify_refs_hook_invoked_after_generation(tmp_path) -> None:
    """When a qualify_refs_service is wired, it runs after generation and bumps
    the progress total from 4 to 5."""
    adapter = FakeAdapter()
    runs: list[Path] = []

    class _StubQualify:
        def run(self, codebase_dir: Path, *, dry_run: bool = False) -> None:  # noqa: ARG002
            runs.append(Path(codebase_dir))

    service = ReverseEngineerService(
        adapter_factory=lambda _cfg: adapter,
        qualify_refs_service=_StubQualify(),  # type: ignore[arg-type]
    )

    progress_log: list[tuple[str, int, int]] = []
    out = service.run(_cfg(), tmp_path, progress=lambda m, c, t: progress_log.append((m, c, t)))

    assert runs and runs[0] == out
    # Total is now 5 (4 base + 1 qualify step).
    assert progress_log[0][2] == 5
    assert progress_log[-1] == ("Готово", 5, 5)
    assert any("Квалификация" in m for m, _, _ in progress_log)


def test_qualify_refs_error_does_not_fail_reverse(tmp_path) -> None:
    """A qualify-refs failure is swallowed — reverse still returns successfully."""
    adapter = FakeAdapter()

    class _BoomQualify:
        def run(self, codebase_dir: Path, *, dry_run: bool = False) -> None:  # noqa: ARG002
            raise RuntimeError("boom")

    service = ReverseEngineerService(
        adapter_factory=lambda _cfg: adapter,
        qualify_refs_service=_BoomQualify(),  # type: ignore[arg-type]
    )
    out = service.run(_cfg(), tmp_path)
    assert out.is_dir()

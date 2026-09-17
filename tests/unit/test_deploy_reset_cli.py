"""Unit tests for the ``db-pm deploy reset`` CLI command (Phase 18).

The service is stubbed via monkeypatch (same pattern as
test_deploy_plan_apply_cli.py). CLI contract under test:

* option plumbing (--dir/--target-connection-file/--output-dir/--dry-run/--yes
  → service), config → service_schema wiring;
* the plan printout + type-the-database-name confirmation (wrong name → exit 1,
  --yes and --dry-run skip the prompt);
* exit codes: 0 ok / 1 rejected (flag missing) / 2 hard error;
* ``allow_drop_schemas: true`` round-trips through the connection YAML.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from db_project_manager.application.schema_reset_service import (
    SchemaResetError,
    SchemaResetRejected,
)
from db_project_manager.presentation.cli import main as cli_main

runner = CliRunner()


def _patch_common(monkeypatch) -> None:
    monkeypatch.setattr(
        cli_main, "load_cfg",
        lambda *a, **k: SimpleNamespace(
            deploy=SimpleNamespace(service_schema="__deploy"),
            logging=SimpleNamespace(level="INFO"),
            paths=SimpleNamespace(logs_dir=None),
        ),
    )
    monkeypatch.setattr(cli_main, "configure_logging", lambda *a, **k: None)


def _conn_file(tmp_path: Path, *, allow: bool = True) -> Path:
    conn = tmp_path / "dev.yaml"
    conn.write_text(
        "name: dev\n"
        "connection_type: direct\n"
        "host: localhost\n"
        "port: 5432\n"
        "database: devdb\n"
        "username: u\n"
        "password: plain-secret\n"
        "type: postgres\n"
        f"allow_drop_schemas: {str(allow).lower()}\n",
        encoding="utf-8",
    )
    return conn


def _stub_plan() -> SimpleNamespace:
    def is_content_drop(schema: str) -> bool:
        return schema in ("public", "cis_app")

    return SimpleNamespace(
        target=SimpleNamespace(name="dev", database="devdb"),
        schemas=["public", "cis_app", "junk_old"],
        object_counts={"public": 5, "cis_app": 40, "junk_old": 7},
        in_codebase={"cis_app"},
        extensions=[{"name": "uuid-ossp", "schema": "public"}],
        is_content_drop=is_content_drop,
    )


def _stub_result() -> SimpleNamespace:
    return SimpleNamespace(
        dry_run=False,
        schemas_wiped=["public", "cis_app"],
        schemas_dropped=["junk_old"],
        extensions_dropped=["uuid-ossp"],
        journal_truncated=True,
    )


class _StubService:
    """Records constructor + collect()/execute() args; returns set outcomes."""

    last: "_StubService | None" = None

    def __init__(self, service_schema: str = "__deploy") -> None:
        self.service_schema = service_schema
        self.collect_calls: list[dict] = []
        self.execute_calls: list[dict] = []
        self.collect_error: Exception | None = None
        self.collect_plan = _stub_plan()
        self.execute_result = _stub_result()
        _StubService.last = self

    def collect(self, codebase_dir, target_cfg) -> SimpleNamespace:
        self.collect_calls.append({
            "codebase_dir": Path(codebase_dir), "target_cfg": target_cfg,
        })
        if self.collect_error is not None:
            raise self.collect_error
        return self.collect_plan

    def execute(self, plan, output_dir, *, dry_run=False, progress=None) -> SimpleNamespace:
        self.execute_calls.append({
            "output_dir": Path(output_dir), "dry_run": dry_run,
        })
        self.execute_result.dry_run = dry_run
        return self.execute_result


def _reset_args(tmp_path: Path, conn: Path, out: Path, *extra: str) -> list[str]:
    return [
        "deploy", "reset",
        "--dir", str(tmp_path / "codebase"),
        "--target-connection-file", str(conn),
        "--output-dir", str(out),
        "--no-run-subdir",
        *extra,
    ]


def _invoke_reset(monkeypatch, tmp_path: Path, *extra: str, allow: bool = True):
    _patch_common(monkeypatch)
    monkeypatch.setattr(cli_main, "SchemaResetService", _StubService)
    return runner.invoke(
        cli_main.app, _reset_args(tmp_path, _conn_file(tmp_path, allow=allow),
                                  tmp_path / "out", *extra)
    )


def test_reset_happy_path_with_yes(monkeypatch, tmp_path: Path) -> None:
    result = _invoke_reset(monkeypatch, tmp_path, "--yes")

    assert result.exit_code == 0, result.output
    stub = _StubService.last
    assert stub.service_schema == "__deploy"
    assert stub.collect_calls[0]["target_cfg"].allow_drop_schemas is True
    assert stub.collect_calls[0]["target_cfg"].database == "devdb"
    assert stub.execute_calls[0]["output_dir"] == tmp_path / "out"
    assert stub.execute_calls[0]["dry_run"] is False
    assert "DROP SCHEMA CASCADE" in result.output
    assert "junk_old" in result.output


def test_reset_without_flag_exits_1(monkeypatch, tmp_path: Path) -> None:
    orig_init = _StubService.__init__

    def _init(self, service_schema="__deploy"):  # noqa: ANN001
        orig_init(self, service_schema)
        self.collect_error = SchemaResetRejected("нет флага")

    monkeypatch.setattr(_StubService, "__init__", _init)
    result = _invoke_reset(monkeypatch, tmp_path, "--yes", allow=False)

    assert result.exit_code == 1
    assert "отклонён" in result.output


def test_reset_hard_error_exits_2(monkeypatch, tmp_path: Path) -> None:
    orig_init = _StubService.__init__

    def _init(self, service_schema="__deploy"):  # noqa: ANN001
        orig_init(self, service_schema)
        self.collect_error = SchemaResetError("тип не совпадает")

    monkeypatch.setattr(_StubService, "__init__", _init)
    result = _invoke_reset(monkeypatch, tmp_path, "--yes")

    assert result.exit_code == 2


def test_reset_confirmation_requires_exact_database_name(
    monkeypatch, tmp_path: Path
) -> None:
    conn = _conn_file(tmp_path)
    out = tmp_path / "out"
    _patch_common(monkeypatch)
    monkeypatch.setattr(cli_main, "SchemaResetService", _StubService)

    wrong = runner.invoke(
        cli_main.app, _reset_args(tmp_path, conn, out), input="other_db\n"
    )
    assert wrong.exit_code == 1
    assert not _StubService.last.execute_calls  # execute never ran

    right = runner.invoke(
        cli_main.app, _reset_args(tmp_path, conn, out), input="devdb\n"
    )
    assert right.exit_code == 0, right.output
    assert _StubService.last.execute_calls


def test_reset_dry_run_skips_prompt_and_passes_flag(
    monkeypatch, tmp_path: Path
) -> None:
    result = _invoke_reset(monkeypatch, tmp_path, "--dry-run")

    assert result.exit_code == 0, result.output
    assert _StubService.last.execute_calls[0]["dry_run"] is True
    assert "Dry-run" in result.output


def test_reset_no_user_schemas_is_success_noop(monkeypatch, tmp_path: Path) -> None:
    orig_init = _StubService.__init__

    def _init(self, service_schema="__deploy"):  # noqa: ANN001
        orig_init(self, service_schema)
        self.collect_plan.schemas = []

    monkeypatch.setattr(_StubService, "__init__", _init)
    result = _invoke_reset(monkeypatch, tmp_path, "--yes")

    assert result.exit_code == 0
    assert "сброс не требуется" in result.output
    assert not _StubService.last.execute_calls

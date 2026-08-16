"""Unit tests for the db-pm deploy plan / deploy apply CLI commands (Phase 12, S7).

The service is stubbed via monkeypatch; the CLI contract under test: option
plumbing (dir/target-connection-file/output-dir/flags → service), exit codes
(0 ok / 1 rejected / 2 hard error) and the config → service_schema wiring.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from db_project_manager.application.deploy_apply_service import (
    DeployApplyError,
    DeployApplyRejected,
)
from db_project_manager.domain.delta import DeltaPlan
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


def _conn_file(tmp_path: Path) -> Path:
    conn = tmp_path / "target.yaml"
    conn.write_text(
        "name: target\n"
        "connection_type: direct\n"
        "host: localhost\n"
        "port: 5432\n"
        "database: target\n"
        "username: u\n"
        "password: plain-secret\n"
        "type: postgres\n",
        encoding="utf-8",
    )
    return conn


@dataclass
class _ApplyResultStub:
    planned: int = 3
    applied: int = 3
    applied_version: str = "2026.08.16.01"
    rehearsal_db: str | None = "dbpm_rehearsal_x"
    output_dir: Path | None = None
    rehearsal_dir: Path | None = None
    artifacts: list = field(default_factory=list)


class _StubService:
    """Records constructor + plan()/apply() args; returns pre-set outcomes."""

    last: "_StubService | None" = None

    def __init__(self, service_schema: str = "__deploy") -> None:
        self.service_schema = service_schema
        self.plan_calls: list[dict] = []
        self.apply_calls: list[dict] = []
        self.plan_result = DeltaPlan(db_type="postgres")
        self.apply_result = _ApplyResultStub()
        self.plan_error: Exception | None = None
        self.apply_error: Exception | None = None
        _StubService.last = self

    def plan(self, codebase_dir, target_cfg, output_dir, *, include_drops=False,
             progress=None):
        self.plan_calls.append({
            "codebase_dir": Path(codebase_dir), "target_cfg": target_cfg,
            "output_dir": Path(output_dir), "include_drops": include_drops,
        })
        if self.plan_error is not None:
            raise self.plan_error
        return self.plan_result

    def apply(self, codebase_dir, target_cfg, output_dir, *, include_drops=False,
              rehearsal=True, keep_rehearsal_db=False, progress=None):
        self.apply_calls.append({
            "codebase_dir": Path(codebase_dir), "target_cfg": target_cfg,
            "output_dir": Path(output_dir), "include_drops": include_drops,
            "rehearsal": rehearsal, "keep_rehearsal_db": keep_rehearsal_db,
        })
        if self.apply_error is not None:
            raise self.apply_error
        return self.apply_result


def _base_args(tmp_path: Path) -> list[str]:
    return [
        "--dir", str(tmp_path / "code"),
        "--target-connection-file", str(_conn_file(tmp_path)),
        "--output-dir", str(tmp_path / "out"),
    ]


def _invoke_plan(monkeypatch, tmp_path: Path, *extra: str):
    _patch_common(monkeypatch)
    monkeypatch.setattr(cli_main, "DeployApplyService", _StubService)
    return runner.invoke(
        cli_main.app, ["deploy", "plan", *_base_args(tmp_path), *extra]
    )


def _invoke_apply(monkeypatch, tmp_path: Path, *extra: str):
    _patch_common(monkeypatch)
    monkeypatch.setattr(cli_main, "DeployApplyService", _StubService)
    return runner.invoke(
        cli_main.app, ["deploy", "apply", *_base_args(tmp_path), *extra]
    )


# ------------------------------------------------------------------- plan


def test_plan_ok_exit_zero(monkeypatch, tmp_path) -> None:
    result = _invoke_plan(monkeypatch, tmp_path)
    assert result.exit_code == 0
    assert "План готов" in result.output
    assert "safe: 0" in result.output


def test_plan_rejected_exit_one(monkeypatch, tmp_path) -> None:
    orig_init = _StubService.__init__

    def _init(self, service_schema="__deploy"):  # noqa: ANN001
        orig_init(self, service_schema)
        self.plan_error = DeployApplyRejected("safety-gate: app.orders")

    monkeypatch.setattr(_StubService, "__init__", _init)
    result = _invoke_plan(monkeypatch, tmp_path)
    assert result.exit_code == 1
    assert "app.orders" in (result.stderr or "") or "app.orders" in result.output


def test_plan_hard_error_exit_two(monkeypatch, tmp_path) -> None:
    orig_init = _StubService.__init__

    def _init(self, service_schema="__deploy"):  # noqa: ANN001
        orig_init(self, service_schema)
        self.plan_error = DeployApplyError("версия новее")

    monkeypatch.setattr(_StubService, "__init__", _init)
    result = _invoke_plan(monkeypatch, tmp_path)
    assert result.exit_code == 2


def test_plan_builds_correct_call(monkeypatch, tmp_path) -> None:
    result = _invoke_plan(monkeypatch, tmp_path, "--include-drops")
    assert result.exit_code == 0
    stub = _StubService.last
    assert stub is not None
    assert stub.service_schema == "__deploy"
    assert len(stub.plan_calls) == 1
    call = stub.plan_calls[0]
    assert call["codebase_dir"] == tmp_path / "code"
    assert call["output_dir"] == tmp_path / "out"
    assert call["include_drops"] is True
    assert call["target_cfg"].database == "target"
    assert call["target_cfg"].password == "plain-secret"


# ------------------------------------------------------------------ apply


def test_apply_ok_exit_zero(monkeypatch, tmp_path) -> None:
    result = _invoke_apply(monkeypatch, tmp_path)
    assert result.exit_code == 0
    assert "Apply завершён" in result.output
    assert "3/3" in result.output
    assert "dbpm_rehearsal_x" in result.output


def test_apply_rejected_exit_one(monkeypatch, tmp_path) -> None:
    orig_init = _StubService.__init__

    def _init(self, service_schema="__deploy"):  # noqa: ANN001
        orig_init(self, service_schema)
        self.apply_error = DeployApplyRejected("CD-11: остаточные не-safe операции")

    monkeypatch.setattr(_StubService, "__init__", _init)
    result = _invoke_apply(monkeypatch, tmp_path)
    assert result.exit_code == 1
    assert "CD-11" in (result.stderr or "") or "CD-11" in result.output


def test_apply_hard_error_exit_two(monkeypatch, tmp_path) -> None:
    orig_init = _StubService.__init__

    def _init(self, service_schema="__deploy"):  # noqa: ANN001
        orig_init(self, service_schema)
        self.apply_error = DeployApplyError("Репетиция: сбой")

    monkeypatch.setattr(_StubService, "__init__", _init)
    result = _invoke_apply(monkeypatch, tmp_path)
    assert result.exit_code == 2


def test_apply_flags_plumb_through(monkeypatch, tmp_path) -> None:
    result = _invoke_apply(
        monkeypatch, tmp_path,
        "--include-drops", "--no-rehearsal", "--keep-rehearsal-db",
    )
    assert result.exit_code == 0
    call = _StubService.last.apply_calls[0]
    assert call["include_drops"] is True
    assert call["rehearsal"] is False
    assert call["keep_rehearsal_db"] is True


def test_apply_without_rehearsal_note(monkeypatch, tmp_path) -> None:
    class _NoRehearsalStub(_StubService):
        def __init__(self, service_schema="__deploy"):  # noqa: ANN001
            super().__init__(service_schema)
            self.apply_result = _ApplyResultStub(rehearsal_db=None)

    _patch_common(monkeypatch)
    monkeypatch.setattr(cli_main, "DeployApplyService", _NoRehearsalStub)
    result = runner.invoke(
        cli_main.app, ["deploy", "apply", *_base_args(tmp_path), "--no-rehearsal"]
    )
    assert result.exit_code == 0
    assert "без репетиции" in result.output


def test_plan_with_blocked_ops_exits_one(monkeypatch, tmp_path) -> None:
    """A plan that documents BLOCKED operations is written, but signals CI (exit 1)."""
    from db_project_manager.domain.delta import (
        OperationClass,
        PlannedOperation,
    )

    orig_init = _StubService.__init__

    def _init(self, service_schema="__deploy"):  # noqa: ANN001
        orig_init(self, service_schema)
        self.plan_result = DeltaPlan(
            db_type="postgres",
            operations=[PlannedOperation(
                object_key="k", object_type="table", object_schema="app",
                object_name="orders", action="alter",
                classification=OperationClass.BLOCKED, reason="нет pre-скрипта",
            )],
        )

    monkeypatch.setattr(_StubService, "__init__", _init)
    result = _invoke_plan(monkeypatch, tmp_path)
    assert result.exit_code == 1
    assert "BLOCKED" in (result.stderr or "") or "BLOCKED" in result.output

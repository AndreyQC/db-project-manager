"""Unit tests for the db-pm deploy analyze CLI command (Phase 11, S6).

The service is stubbed via monkeypatch; the CLI contract under test: option
plumbing (dir/target-connection-file/output-dir → service), exit codes
(0 clean / 1 violations / 2 hard error) and the config → service_schema wiring.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from db_project_manager.domain.safety import SafetyGateVerdict
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


class _StubService:
    """Records constructor + analyze() args; returns a pre-set verdict."""

    last: "_StubService | None" = None

    def __init__(self, service_schema: str = "__deploy") -> None:
        self.service_schema = service_schema
        self.calls: list[dict] = []
        self.verdict = SafetyGateVerdict(clean=True, db_type="postgres", touched=[])
        self.error: Exception | None = None
        _StubService.last = self

    def analyze(self, codebase_dir, target_cfg, output_dir, progress=None):  # noqa: ARG002
        self.calls.append(
            {"codebase_dir": Path(codebase_dir), "target_cfg": target_cfg,
             "output_dir": Path(output_dir)}
        )
        if self.error is not None:
            raise self.error
        return self.verdict


def _invoke(monkeypatch, tmp_path: Path) -> "any":
    _patch_common(monkeypatch)
    monkeypatch.setattr(cli_main, "SafetyGateService", _StubService)
    return runner.invoke(
        cli_main.app,
        [
            "deploy", "analyze",
            "--dir", str(tmp_path / "code"),
            "--target-connection-file", str(_conn_file(tmp_path)),
            "--output-dir", str(tmp_path / "report"),
        ],
    )


def test_clean_exit_zero(monkeypatch, tmp_path) -> None:
    result = _invoke(monkeypatch, tmp_path)
    assert result.exit_code == 0
    assert "CLEAN" in result.output


def test_violation_exit_one(monkeypatch, tmp_path) -> None:
    orig_init = _StubService.__init__

    def _init(self, service_schema="__deploy"):  # noqa: ANN001
        orig_init(self, service_schema)
        self.verdict = SafetyGateVerdict(clean=False, db_type="postgres", touched=[])

    monkeypatch.setattr(_StubService, "__init__", _init)
    result = _invoke(monkeypatch, tmp_path)
    assert result.exit_code == 1
    assert "VIOLATIONS" in result.output or "VIOLATIONS" in (result.stderr or "")


def test_hard_error_exit_two(monkeypatch, tmp_path) -> None:
    from db_project_manager.application.safety_gate_service import SafetyGateError

    orig_init = _StubService.__init__

    def _init(self, service_schema="__deploy"):  # noqa: ANN001
        orig_init(self, service_schema)
        self.error = SafetyGateError("target newer than source")

    monkeypatch.setattr(_StubService, "__init__", _init)
    result = _invoke(monkeypatch, tmp_path)
    assert result.exit_code == 2
    assert "target newer" in (result.stderr or "") or "target newer" in result.output


def test_cli_builds_correct_call(monkeypatch, tmp_path) -> None:
    result = _invoke(monkeypatch, tmp_path)
    assert result.exit_code == 0
    stub = _StubService.last
    assert stub is not None
    assert stub.service_schema == "__deploy"  # wired from config (DeployConfig)
    assert len(stub.calls) == 1
    call = stub.calls[0]
    assert call["codebase_dir"] == tmp_path / "code"
    # Phase 15.7: the CLI writes into a per-run subdirectory under --output-dir
    # (unless --no-run-subdir); the service receives that concrete run dir.
    assert call["output_dir"].parent == tmp_path / "report"
    assert call["output_dir"].is_dir()
    assert call["target_cfg"].database == "target"
    assert call["target_cfg"].type == "postgres"
    assert call["target_cfg"].password == "plain-secret"


def test_no_run_subdir_passes_output_dir_flat(monkeypatch, tmp_path) -> None:
    """Phase 15.7: --no-run-subdir keeps the legacy flat layout."""
    _patch_common(monkeypatch)
    monkeypatch.setattr(cli_main, "SafetyGateService", _StubService)
    result = runner.invoke(
        cli_main.app,
        [
            "deploy", "analyze",
            "--dir", str(tmp_path / "code"),
            "--target-connection-file", str(_conn_file(tmp_path)),
            "--output-dir", str(tmp_path / "report"),
            "--no-run-subdir",
        ],
    )
    assert result.exit_code == 0
    assert _StubService.last.calls[0]["output_dir"] == tmp_path / "report"


def test_missing_connection_file_exit_two(monkeypatch, tmp_path) -> None:
    _patch_common(monkeypatch)
    monkeypatch.setattr(cli_main, "SafetyGateService", _StubService)
    result = runner.invoke(
        cli_main.app,
        [
            "deploy", "analyze",
            "--dir", str(tmp_path),
            "--target-connection-file", str(tmp_path / "missing.yaml"),
            "--output-dir", str(tmp_path / "out"),
        ],
    )
    assert result.exit_code == 2
    assert "не найден" in (result.stderr or "") or "не найден" in result.output

"""Unit tests for the db-pm compare run CLI command (Phase 9)."""

from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from db_project_manager.domain.diff import CodebaseManifest
from db_project_manager.infrastructure.config.codebase_manifest import write_manifest
from db_project_manager.presentation.cli import main as cli_main

runner = CliRunner()

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
CODEBASE_SAMPLE = FIXTURES / "codebase_sample"


def _copy_fixture_with_manifest(tmp_path: Path, name: str, db_type: str = "postgres") -> Path:
    dest = tmp_path / name
    shutil.copytree(CODEBASE_SAMPLE, dest)
    write_manifest(
        CodebaseManifest(db_type=db_type, database="demo", generated_at="2026-07-29T00:00:00+00:00"),
        dest,
    )
    return dest


def _patch_common(monkeypatch):
    """Patch logging/config so the CLI does not need a real config.yaml."""
    monkeypatch.setattr(cli_main, "load_cfg", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(cli_main, "configure_logging", lambda *a, **k: None)


def test_both_source_flags_set_exits_2(tmp_path, monkeypatch):
    """--source-dir and --source-connection-file together → exit code 2."""
    _patch_common(monkeypatch)
    result = runner.invoke(
        cli_main.app,
        [
            "compare", "run",
            "--source-dir", str(tmp_path),
            "--source-connection-file", "connections/x.yaml",
            "--target-dir", str(tmp_path),
            "--output-dir", str(tmp_path / "out"),
        ],
    )
    assert result.exit_code == 2
    assert "ровно один" in result.output or "ровно один" in (result.stderr or "")


def test_no_source_flag_set_exits_2(tmp_path, monkeypatch):
    """Neither source flag → exit code 2."""
    _patch_common(monkeypatch)
    result = runner.invoke(
        cli_main.app,
        [
            "compare", "run",
            "--target-dir", str(tmp_path),
            "--output-dir", str(tmp_path / "out"),
        ],
    )
    assert result.exit_code == 2


def test_source_dir_not_exists_exits_2(tmp_path, monkeypatch):
    """A non-existent --source-dir → exit code 2."""
    _patch_common(monkeypatch)
    result = runner.invoke(
        cli_main.app,
        [
            "compare", "run",
            "--source-dir", str(tmp_path / "does_not_exist"),
            "--target-dir", str(tmp_path),
            "--output-dir", str(tmp_path / "out"),
        ],
    )
    assert result.exit_code == 2
    assert "не существует" in result.output or "не существует" in (result.stderr or "")


def test_dir_vs_dir_identical_writes_report(tmp_path, monkeypatch):
    """Two identical fixture copies with manifests → exit 0, report written."""
    _patch_common(monkeypatch)
    src = _copy_fixture_with_manifest(tmp_path, "src")
    tgt = _copy_fixture_with_manifest(tmp_path, "tgt")
    out = tmp_path / "report"

    fake_service = SimpleNamespace(run=lambda *a, **k: out)
    # CompareService is imported lazily inside compare_run from compare_service,
    # so patch the attribute on that module.
    import db_project_manager.application.compare_service as cs_module

    monkeypatch.setattr(cs_module, "CompareService", lambda: fake_service)

    result = runner.invoke(
        cli_main.app,
        [
            "compare", "run",
            "--source-dir", str(src),
            "--target-dir", str(tgt),
            "--output-dir", str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Отчёт сравнения" in result.output


def test_compare_error_exits_2(tmp_path, monkeypatch):
    """A CompareService raising CompareError → exit code 2, red message."""
    _patch_common(monkeypatch)

    class _Boom:
        def run(self, *a, **k):
            from db_project_manager.application.compare_service import CompareError

            raise CompareError("Несовместимые типы БД: source=postgres, target=greenplum")

    import db_project_manager.application.compare_service as cs_module

    monkeypatch.setattr(cs_module, "CompareService", lambda: _Boom())

    src = _copy_fixture_with_manifest(tmp_path, "src")
    tgt = _copy_fixture_with_manifest(tmp_path, "tgt")
    result = runner.invoke(
        cli_main.app,
        [
            "compare", "run",
            "--source-dir", str(src),
            "--target-dir", str(tgt),
            "--output-dir", str(tmp_path / "out"),
        ],
    )
    assert result.exit_code == 2
    assert "Несовместимые типы БД" in result.output or "Несовместимые типы БД" in (result.stderr or "")


def test_compare_help_lists_subcommand(tmp_path):
    """db-pm compare --help shows the 'run' subcommand."""
    result = runner.invoke(cli_main.app, ["compare", "--help"])
    assert result.exit_code == 0
    assert "run" in result.output

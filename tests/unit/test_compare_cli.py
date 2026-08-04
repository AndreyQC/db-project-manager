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


# --- compare report (Phase 14 S2) ---


def _write_synthetic_report(path: Path) -> None:
    """Write a minimal valid diff_report.json for `compare report` tests."""
    from db_project_manager.domain.diff import (
        DiffEntry,
        DiffReport,
        DiffStatus,
        ObjectSnapshot,
        SnapshotSourceKind,
        StateSnapshot,
    )

    obj = ObjectSnapshot(
        object_key="k1",
        object_schema="public",
        object_name="new_table",
        object_type="table",
        sql_normalized="CREATE TABLE new_table (a INT)",
        sql_hash="aaaa0000",
        estimated_rows=42,
    )
    report = DiffReport(
        source=StateSnapshot(
            source_kind=SnapshotSourceKind.DIR,
            source_ref="src_dir",
            db_type="postgres",
            generated_at="2026-08-04T00:00:00+00:00",
            objects={"k1": obj},
        ),
        target=StateSnapshot(
            source_kind=SnapshotSourceKind.DIR,
            source_ref="tgt_dir",
            db_type="postgres",
            generated_at="2026-08-04T00:00:00+00:00",
            objects={},
        ),
        generated_at="2026-08-04T00:00:00+00:00",
        summary={"added": 1, "removed": 0, "changed": 0, "unchanged": 0},
        entries=[DiffEntry(object_key="k1", status=DiffStatus.ADDED, source_snapshot=obj)],
    )
    path.write_text(report.model_dump_json(), encoding="utf-8")


def test_compare_report_help_lists_command():
    """db-pm compare --help shows the 'report' subcommand."""
    result = runner.invoke(cli_main.app, ["compare", "--help"])
    assert result.exit_code == 0
    assert "report" in result.output


def test_compare_report_writes_markdown_next_to_source(tmp_path, monkeypatch):
    """`compare report --from <json>` writes diff_report.md next to it."""
    _patch_common(monkeypatch)
    src = tmp_path / "diff_report.json"
    _write_synthetic_report(src)

    result = runner.invoke(
        cli_main.app,
        ["compare", "report", "--from", str(src)],
    )
    assert result.exit_code == 0, result.output
    out = tmp_path / "diff_report.md"
    assert out.is_file()
    assert "# Diff report" in out.read_text(encoding="utf-8")
    assert "Markdown-отчёт" in result.output


def test_compare_report_custom_output(tmp_path, monkeypatch):
    """`--output` writes the markdown to the given path."""
    _patch_common(monkeypatch)
    src = tmp_path / "diff_report.json"
    _write_synthetic_report(src)
    custom = tmp_path / "nested" / "custom.md"

    result = runner.invoke(
        cli_main.app,
        ["compare", "report", "--from", str(src), "--output", str(custom)],
    )
    assert result.exit_code == 0, result.output
    assert custom.is_file()


def test_compare_report_missing_file_exits_2(tmp_path, monkeypatch):
    """A non-existent --from → exit code 2."""
    _patch_common(monkeypatch)
    result = runner.invoke(
        cli_main.app,
        ["compare", "report", "--from", str(tmp_path / "nope.json")],
    )
    assert result.exit_code == 2
    assert "не найден" in result.output.lower() or "не найден" in (result.stderr or "").lower()


def test_compare_report_invalid_json_exits_2(tmp_path, monkeypatch):
    """Garbage at --from → exit code 2."""
    _patch_common(monkeypatch)
    src = tmp_path / "diff_report.json"
    src.write_text("{not valid json", encoding="utf-8")

    result = runner.invoke(
        cli_main.app,
        ["compare", "report", "--from", str(src)],
    )
    assert result.exit_code == 2

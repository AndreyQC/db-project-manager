"""Unit tests for action CLI builders + contract with the real typer CLI.

The contract test feeds each generated command through typer's CliRunner with
services mocked out: if the GUI builds a flag the CLI does not know, the parse
fails here (guard against GUI<->CLI drift).
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from db_project_manager.infrastructure.config.connection_store import ConnectionStore
from db_project_manager.presentation.cli import main as cli_main
from db_project_manager.presentation.gui.actions.cli import (
    build_cli_compare,
    build_cli_deploy_analyze,
    build_cli_deploy_apply,
    build_cli_deploy_plan,
    build_cli_deploy_validate,
    build_cli_graph_prepare,
    build_cli_reverse_engineer,
)
from db_project_manager.presentation.gui.actions.models import (
    CompareSettings,
    DeployAnalyzeSettings,
    DeployApplySettings,
    DeployValidateSettings,
    GraphPrepareSettings,
    ReverseEngineerSettings,
)

runner = CliRunner()


def _store(tmp_path: Path) -> ConnectionStore:
    return ConnectionStore(tmp_path / "connections")


def _argv(command: str) -> list[str]:
    """Split a generated command into argv (posix=False keeps Windows paths)."""
    tokens = [token.strip('"') for token in shlex.split(command, posix=False)]
    return tokens[1:]  # strip the "db-pm" program name


# --- exact string tests ---


def test_reverse_engineer_cli_string(tmp_path):
    store = _store(tmp_path)
    s = ReverseEngineerSettings(connection="qr", output_dir="C:/out/qr")
    cmd = build_cli_reverse_engineer(s, store)
    assert cmd == (
        f"db-pm reverse-engineer --connection-file {store.path_for('qr')} --output C:/out/qr"
    )


def test_deploy_validate_cli_string_flags_on(tmp_path):
    store = _store(tmp_path)
    s = DeployValidateSettings(
        codebase_dir="C:/out/qr",
        connection="srv",
        prefix="qr",
        keep_db=True,
        continue_on_error=True,
    )
    cmd = build_cli_deploy_validate(s, store)
    assert cmd == (
        f"db-pm deploy validate --dir C:/out/qr "
        f"--connection-file {store.path_for('srv')} "
        f"--prefix qr --keep-db --continue-on-error"
    )


def test_deploy_validate_cli_string_flags_off_omitted(tmp_path):
    store = _store(tmp_path)
    s = DeployValidateSettings(codebase_dir="C:/out/qr", connection="srv")
    cmd = build_cli_deploy_validate(s, store)
    assert "--keep-db" not in cmd
    assert "--continue-on-error" not in cmd
    assert "--prefix" not in cmd


def test_graph_prepare_cli_string_full(tmp_path):
    store = _store(tmp_path)
    s = GraphPrepareSettings(codebase_dir="C:/out/qr", format="graphml", validate_graph=True)
    cmd = build_cli_graph_prepare(s, store)
    assert cmd == (
        "db-pm graph build --dir C:/out/qr && "
        "db-pm graph export --dir C:/out/qr --format graphml && "
        "db-pm graph validate --dir C:/out/qr"
    )


def test_graph_prepare_build_only(tmp_path):
    store = _store(tmp_path)
    s = GraphPrepareSettings(codebase_dir="C:/out/qr", format="none", validate_graph=False)
    assert build_cli_graph_prepare(s, store) == "db-pm graph build --dir C:/out/qr"


def test_graph_prepare_custom_output_dir(tmp_path):
    store = _store(tmp_path)
    s = GraphPrepareSettings(
        codebase_dir="C:/out/qr",
        format="graphml",
        validate_graph=False,
        output_dir="C:/graphs",
    )
    cmd = build_cli_graph_prepare(s, store)
    assert cmd == (
        "db-pm graph build --dir C:/out/qr && "
        "db-pm graph export --dir C:/out/qr --format graphml --output C:/graphs/graph.graphml"
    )


def test_paths_with_spaces_are_quoted(tmp_path):
    store = _store(tmp_path)
    s = ReverseEngineerSettings(connection="qr", output_dir="C:/my dir/qr")
    cmd = build_cli_reverse_engineer(s, store)
    assert '--output "C:/my dir/qr"' in cmd


# --- compare (Phase 9 GUI action) ---


def test_compare_cli_string_dir_vs_dir(tmp_path):
    store = _store(tmp_path)
    s = CompareSettings(source_dir="C:/src", target_dir="C:/tgt", output_dir="C:/out")
    cmd = build_cli_compare(s, store)
    assert cmd == "db-pm compare run --output-dir C:/out --source-dir C:/src --target-dir C:/tgt"


def test_compare_cli_string_db_vs_db(tmp_path):
    store = _store(tmp_path)
    s = CompareSettings(
        source_connection="dev", target_connection="prod", output_dir="C:/out"
    )
    cmd = build_cli_compare(s, store)
    assert cmd == (
        f"db-pm compare run --output-dir C:/out "
        f"--source-connection-file {store.path_for('dev')} "
        f"--target-connection-file {store.path_for('prod')}"
    )


def test_compare_cli_string_mixed_dir_db(tmp_path):
    store = _store(tmp_path)
    s = CompareSettings(
        source_dir="C:/src", target_connection="prod", output_dir="C:/out"
    )
    cmd = build_cli_compare(s, store)
    assert cmd == (
        f"db-pm compare run --output-dir C:/out --source-dir C:/src "
        f"--target-connection-file {store.path_for('prod')}"
    )


def test_compare_cli_keep_model_dir_flag(tmp_path):
    store = _store(tmp_path)
    s = CompareSettings(
        source_dir="C:/src", target_dir="C:/tgt", output_dir="C:/out", keep_model_dir=True
    )
    cmd = build_cli_compare(s, store)
    assert "--keep-model-dir" in cmd


def test_compare_cli_keep_model_dir_omitted_when_off(tmp_path):
    store = _store(tmp_path)
    s = CompareSettings(source_dir="C:/src", target_dir="C:/tgt", output_dir="C:/out")
    cmd = build_cli_compare(s, store)
    assert "--keep-model-dir" not in cmd


def test_compare_cli_paths_with_spaces_quoted(tmp_path):
    store = _store(tmp_path)
    s = CompareSettings(
        source_dir="C:/my src", target_dir="C:/my tgt", output_dir="C:/my out"
    )
    cmd = build_cli_compare(s, store)
    assert '--source-dir "C:/my src"' in cmd
    assert '--target-dir "C:/my tgt"' in cmd
    assert '--output-dir "C:/my out"' in cmd


def test_compare_cli_omits_unset_side(tmp_path):
    """Neither source field set → --source-* omitted; CLI will exit 2 with a clear message."""
    store = _store(tmp_path)
    s = CompareSettings(target_dir="C:/tgt", output_dir="C:/out")
    cmd = build_cli_compare(s, store)
    assert "--source-" not in cmd
    assert "--target-dir C:/tgt" in cmd


# --- deploy analyze (Phase 11 GUI action) ---


def test_deploy_analyze_cli_string(tmp_path):
    store = _store(tmp_path)
    s = DeployAnalyzeSettings(
        codebase_dir="C:/out/qr", target_connection="prod", output_dir="C:/reports"
    )
    cmd = build_cli_deploy_analyze(s, store)
    assert cmd == (
        f"db-pm deploy analyze --dir C:/out/qr "
        f"--target-connection-file {store.path_for('prod')} "
        f"--output-dir C:/reports"
    )


def test_deploy_analyze_cli_paths_with_spaces_quoted(tmp_path):
    store = _store(tmp_path)
    s = DeployAnalyzeSettings(
        codebase_dir="C:/my code", target_connection="prod", output_dir="C:/my reports"
    )
    cmd = build_cli_deploy_analyze(s, store)
    assert '--dir "C:/my code"' in cmd
    assert '--output-dir "C:/my reports"' in cmd


# --- typer contract tests (mocked services) ---


def _patch_common(monkeypatch):
    monkeypatch.setattr(cli_main, "_load_connection", lambda path: object())
    monkeypatch.setattr(cli_main, "configure_logging", lambda **kwargs: None)


def test_contract_reverse_engineer(tmp_path, monkeypatch):
    _patch_common(monkeypatch)
    fake_service = SimpleNamespace(run=lambda conn, out, progress=None: Path(out))
    monkeypatch.setattr(cli_main, "build_default_service", lambda: fake_service)

    store = _store(tmp_path)
    cmd = build_cli_reverse_engineer(
        ReverseEngineerSettings(connection="qr", output_dir=str(tmp_path / "out")), store
    )
    result = runner.invoke(cli_main.app, _argv(cmd))
    assert result.exit_code == 0, result.output


def test_contract_deploy_validate(tmp_path, monkeypatch):
    _patch_common(monkeypatch)
    deploy_result = SimpleNamespace(
        success=True, db_name="tmp_db", objects_done=3, objects_total=3, errors=[]
    )
    fake_service = SimpleNamespace(run=lambda *a, **k: deploy_result)
    monkeypatch.setattr(cli_main, "DeployValidateService", lambda: fake_service)

    store = _store(tmp_path)
    cmd = build_cli_deploy_validate(
        DeployValidateSettings(
            codebase_dir=str(tmp_path),
            connection="srv",
            prefix="qr",
            keep_db=True,
            continue_on_error=True,
        ),
        store,
    )
    result = runner.invoke(cli_main.app, _argv(cmd))
    assert result.exit_code == 0, result.output


def test_contract_graph_prepare(tmp_path, monkeypatch):
    _patch_common(monkeypatch)
    fake_graph = SimpleNamespace(vertices={}, edges=[], dangling_edges=lambda: [])
    fake_build_service = SimpleNamespace(
        build_and_store=lambda d: tmp_path / ".dbm_graph",
        build=lambda d: fake_graph,
    )
    monkeypatch.setattr(cli_main, "BuildGraphService", lambda: fake_build_service)
    monkeypatch.setattr(cli_main.graph_store, "read_graph", lambda d: fake_graph)
    monkeypatch.setattr(
        cli_main.graph_store, "graph_dir_for", lambda d: tmp_path / ".dbm_graph"
    )
    monkeypatch.setattr(cli_main, "export_graph", lambda g, fmt, out: Path(out))

    store = _store(tmp_path)
    cmd = build_cli_graph_prepare(
        GraphPrepareSettings(
            codebase_dir=str(tmp_path),
            format="graphml",
            validate_graph=True,
            output_dir=str(tmp_path / "graphs"),
        ),
        store,
    )
    for subcommand in cmd.split(" && "):
        result = runner.invoke(cli_main.app, _argv(subcommand))
        assert result.exit_code == 0, f"{subcommand}: {result.output}"


def test_contract_compare(tmp_path, monkeypatch):
    """The GUI-built compare command parses through the real typer compare run."""
    _patch_common(monkeypatch)
    # compare_run calls load_cfg(...) — patch it to avoid needing config.yaml.
    monkeypatch.setattr(cli_main, "load_cfg", lambda *a, **k: SimpleNamespace())
    # CompareService is imported lazily inside compare_run, so patch on the source module.
    import db_project_manager.application.compare_service as cs_module

    monkeypatch.setattr(cs_module, "CompareService", lambda: SimpleNamespace(run=lambda *a, **k: tmp_path / "report"))
    # _resolve_side checks is_dir() — create the dirs so the CLI accepts them.
    (tmp_path / "src").mkdir()
    (tmp_path / "tgt").mkdir()

    store = _store(tmp_path)
    cmd = build_cli_compare(
        CompareSettings(
            source_dir=str(tmp_path / "src"),
            target_dir=str(tmp_path / "tgt"),
            output_dir=str(tmp_path / "out"),
            keep_model_dir=True,
        ),
        store,
    )
    result = runner.invoke(cli_main.app, _argv(cmd))
    assert result.exit_code == 0, result.output


def test_contract_deploy_analyze(tmp_path, monkeypatch):
    """The GUI-built deploy analyze command parses through the real typer CLI."""
    from db_project_manager.domain.safety import SafetyGateVerdict

    _patch_common(monkeypatch)
    monkeypatch.setattr(
        cli_main, "load_cfg",
        lambda *a, **k: SimpleNamespace(
            deploy=SimpleNamespace(service_schema="__deploy"),
            logging=SimpleNamespace(level="INFO"),
            paths=SimpleNamespace(logs_dir=None),
        ),
    )
    fake_service = SimpleNamespace(
        analyze=lambda *a, **k: SafetyGateVerdict(clean=True, db_type="postgres", touched=[])
    )
    monkeypatch.setattr(cli_main, "SafetyGateService", lambda **kwargs: fake_service)

    store = _store(tmp_path)
    cmd = build_cli_deploy_analyze(
        DeployAnalyzeSettings(
            codebase_dir=str(tmp_path),
            target_connection="prod",
            output_dir=str(tmp_path / "report"),
        ),
        store,
    )
    result = runner.invoke(cli_main.app, _argv(cmd))
    assert result.exit_code == 0, result.output
    assert "CLEAN" in result.output


# --- Phase 15: deploy plan / deploy apply ---


@dataclass
class _FakeDeltaPlan:
    db_type: str = "postgres"
    source_version: str | None = None
    target_version: str | None = None
    operations: list = ()
    include_drops: bool = False

    @property
    def safe_ops(self):
        return [o for o in self.operations if getattr(o, "classification", None) == "safe"]

    @property
    def needs_pre_ops(self):
        return [o for o in self.operations if getattr(o, "classification", None) == "needs_pre"]

    @property
    def violations(self):
        return [o for o in self.operations if getattr(o, "classification", None) == "blocked"]


@dataclass
class _FakeApplyResult:
    planned: int = 1
    applied: int = 1
    applied_version: str | None = "v1"
    rehearsal_db: str | None = "dbpm_rehearsal_xxx"
    output_dir: Path | None = None


def test_deploy_plan_cli_string_basic(tmp_path):
    """build_cli_deploy_plan: only ``--include-drops`` is optional; other flags required."""
    store = _store(tmp_path)
    s = DeployApplySettings(
        codebase_dir=str(tmp_path / "code"),
        target_connection="prod",
        output_dir=str(tmp_path / "out"),
    )
    cmd = build_cli_deploy_plan(s, store)
    assert cmd == (
        f"db-pm deploy plan --dir {tmp_path / 'code'} "
        f"--target-connection-file {store.path_for('prod')} "
        f"--output-dir {tmp_path / 'out'}"
    )
    assert "--include-drops" not in cmd
    assert "--no-rehearsal" not in cmd  # plan-only flag


def test_deploy_plan_cli_string_with_include_drops(tmp_path):
    store = _store(tmp_path)
    s = DeployApplySettings(
        codebase_dir=str(tmp_path / "code"),
        target_connection="prod",
        output_dir=str(tmp_path / "out"),
        include_drops=True,
    )
    cmd = build_cli_deploy_plan(s, store)
    assert "--include-drops" in cmd


def test_deploy_apply_cli_string_all_three_flags(tmp_path):
    """build_cli_deploy_apply: --include-drops, --no-rehearsal, --keep-rehearsal-db."""
    store = _store(tmp_path)
    s = DeployApplySettings(
        codebase_dir=str(tmp_path / "code"),
        target_connection="prod",
        output_dir=str(tmp_path / "out"),
        include_drops=True,
        no_rehearsal=True,
        keep_rehearsal_db=True,
        confirm_understands_risk=True,  # GUI-side gate; must NOT appear in CLI
    )
    cmd = build_cli_deploy_apply(s, store)
    assert "--include-drops" in cmd
    assert "--no-rehearsal" in cmd
    assert "--keep-rehearsal-db" in cmd
    # GUI gate must not leak into the CLI string (PRE-2 contract).
    assert "confirm_understands_risk" not in cmd


def test_deploy_apply_cli_string_flags_off_omitted(tmp_path):
    store = _store(tmp_path)
    s = DeployApplySettings(
        codebase_dir=str(tmp_path / "code"),
        target_connection="prod",
        output_dir=str(tmp_path / "out"),
    )
    cmd = build_cli_deploy_apply(s, store)
    assert "--include-drops" not in cmd
    assert "--no-rehearsal" not in cmd
    assert "--keep-rehearsal-db" not in cmd


def test_deploy_apply_settings_extra_ignored():
    """``DeployApplySettings`` must accept unknown keys (extra='ignore') — same contract
    as the rest of the GUI settings models (gui_settings.json forward-compat)."""
    s = DeployApplySettings.model_validate({"unknown_field": "ignored"})
    assert s.codebase_dir == ""
    assert s.confirm_understands_risk is False


def test_contract_deploy_plan(tmp_path, monkeypatch):
    """GUI-built ``deploy plan`` command parses through the real typer CLI."""
    from db_project_manager.domain.delta import DeltaPlan

    _patch_common(monkeypatch)
    monkeypatch.setattr(
        cli_main, "load_cfg",
        lambda *a, **k: SimpleNamespace(
            deploy=SimpleNamespace(service_schema="__deploy"),
            logging=SimpleNamespace(level="INFO"),
            paths=SimpleNamespace(logs_dir=None),
        ),
    )
    fake_plan = DeltaPlan(db_type="postgres", operations=[])
    fake_service = SimpleNamespace(plan=lambda *a, **k: fake_plan)
    monkeypatch.setattr(cli_main, "DeployApplyService", lambda **kwargs: fake_service)

    store = _store(tmp_path)
    cmd = build_cli_deploy_plan(
        DeployApplySettings(
            codebase_dir=str(tmp_path / "code"),
            target_connection="prod",
            output_dir=str(tmp_path / "out"),
        ),
        store,
    )
    result = runner.invoke(cli_main.app, _argv(cmd))
    assert result.exit_code == 0, result.output
    assert "План готов" in result.output


def test_contract_deploy_apply(tmp_path, monkeypatch):
    """GUI-built ``deploy apply`` command parses through the real typer CLI."""
    from db_project_manager.application.deploy_apply_service import ApplyResult

    _patch_common(monkeypatch)
    monkeypatch.setattr(
        cli_main, "load_cfg",
        lambda *a, **k: SimpleNamespace(
            deploy=SimpleNamespace(service_schema="__deploy"),
            logging=SimpleNamespace(level="INFO"),
            paths=SimpleNamespace(logs_dir=None),
        ),
    )
    fake_result = ApplyResult(
        planned=1,
        applied=1,
        applied_version="v1",
        rehearsal_db="dbpm_rehearsal_x",
        output_dir=tmp_path / "out",
    )
    fake_service = SimpleNamespace(apply=lambda *a, **k: fake_result)
    monkeypatch.setattr(cli_main, "DeployApplyService", lambda **kwargs: fake_service)

    store = _store(tmp_path)
    cmd = build_cli_deploy_apply(
        DeployApplySettings(
            codebase_dir=str(tmp_path / "code"),
            target_connection="prod",
            output_dir=str(tmp_path / "out"),
            no_rehearsal=True,
        ),
        store,
    )
    result = runner.invoke(cli_main.app, _argv(cmd))
    assert result.exit_code == 0, result.output
    assert "Apply завершён" in result.output

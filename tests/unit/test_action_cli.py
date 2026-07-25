"""Unit tests for action CLI builders + contract with the real typer CLI.

The contract test feeds each generated command through typer's CliRunner with
services mocked out: if the GUI builds a flag the CLI does not know, the parse
fails here (guard against GUI<->CLI drift).
"""

from __future__ import annotations

import shlex
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from db_project_manager.infrastructure.config.connection_store import ConnectionStore
from db_project_manager.presentation.cli import main as cli_main
from db_project_manager.presentation.gui.actions.cli import (
    build_cli_deploy_validate,
    build_cli_graph_prepare,
    build_cli_reverse_engineer,
)
from db_project_manager.presentation.gui.actions.models import (
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


def test_paths_with_spaces_are_quoted(tmp_path):
    store = _store(tmp_path)
    s = ReverseEngineerSettings(connection="qr", output_dir="C:/my dir/qr")
    cmd = build_cli_reverse_engineer(s, store)
    assert '--output "C:/my dir/qr"' in cmd


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
        GraphPrepareSettings(codebase_dir=str(tmp_path), format="graphml", validate_graph=True),
        store,
    )
    for subcommand in cmd.split(" && "):
        result = runner.invoke(cli_main.app, _argv(subcommand))
        assert result.exit_code == 0, f"{subcommand}: {result.output}"

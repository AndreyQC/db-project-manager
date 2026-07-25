"""Build CLI command strings for GUI actions ("Copy CLI" feature).

Pure functions: settings model + ConnectionStore -> shell command string that
mirrors the real typer interface (presentation/cli/main.py). Kept in sync with
the CLI by tests/unit/test_action_cli.py (CliRunner contract test).
"""

from __future__ import annotations

from db_project_manager.infrastructure.config.connection_store import ConnectionStore

from db_project_manager.presentation.gui.actions.models import (
    FORMAT_NONE,
    DeployValidateSettings,
    GraphPrepareSettings,
    ReverseEngineerSettings,
)


def _quote(value: str) -> str:
    """Wrap a path/argument in double quotes if it contains spaces."""
    return f'"{value}"' if " " in value else value


def build_cli_reverse_engineer(settings: ReverseEngineerSettings, store: ConnectionStore) -> str:
    conn_file = store.path_for(settings.connection)
    return (
        f"db-pm reverse-engineer --connection-file {_quote(str(conn_file))} "
        f"--output {_quote(settings.output_dir)}"
    )


def build_cli_deploy_validate(settings: DeployValidateSettings, store: ConnectionStore) -> str:
    conn_file = store.path_for(settings.connection)
    parts = [
        "db-pm deploy validate",
        f"--dir {_quote(settings.codebase_dir)}",
        f"--connection-file {_quote(str(conn_file))}",
    ]
    if settings.prefix:
        parts.append(f"--prefix {_quote(settings.prefix)}")
    if settings.keep_db:
        parts.append("--keep-db")
    if settings.continue_on_error:
        parts.append("--continue-on-error")
    return " ".join(parts)


def build_cli_graph_prepare(settings: GraphPrepareSettings, store: ConnectionStore) -> str:
    del store  # graph actions do not use a connection
    directory = _quote(settings.codebase_dir)
    commands = [f"db-pm graph build --dir {directory}"]
    if settings.format != FORMAT_NONE:
        export_cmd = f"db-pm graph export --dir {directory} --format {settings.format}"
        if settings.output_dir:
            export_file = f"{settings.output_dir}/graph.{settings.format}"
            export_cmd += f" --output {_quote(export_file)}"
        commands.append(export_cmd)
    if settings.validate_graph:
        commands.append(f"db-pm graph validate --dir {directory}")
    return " && ".join(commands)

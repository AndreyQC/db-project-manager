"""Build CLI command strings for GUI actions ("Copy CLI" feature).

Pure functions: settings model + ConnectionStore -> shell command string that
mirrors the real typer interface (presentation/cli/main.py). Kept in sync with
the CLI by tests/unit/test_action_cli.py (CliRunner contract test).
"""

from __future__ import annotations

from db_project_manager.infrastructure.config.connection_store import ConnectionStore

from db_project_manager.presentation.gui.actions.models import (
    FORMAT_NONE,
    CompareSettings,
    DeployAnalyzeSettings,
    DeployApplySettings,
    DeployInitServiceSchemaSettings,
    DeployValidateSettings,
    GraphPrepareSettings,
    ReverseEngineerSettings,
    YamlApplySettings,
    YamlGenerateSettings,
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


def build_cli_deploy_analyze(settings: DeployAnalyzeSettings, store: ConnectionStore) -> str:
    conn_file = store.path_for(settings.target_connection)
    return (
        f"db-pm deploy analyze "
        f"--dir {_quote(settings.codebase_dir)} "
        f"--target-connection-file {_quote(str(conn_file))} "
        f"--output-dir {_quote(settings.output_dir)}"
    )


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


def _side_cli(
    label: str, connection: str, dir_: str, store: ConnectionStore
) -> list[str]:
    """Emit exactly one of ``--<label>-connection-file`` / ``--<label>-dir``.

    Mirrors the CLI's ``_resolve_side`` XOR rule: exactly one must be set. When
    neither is set, the side is omitted — the CLI will then exit 2 with a clear
    message ("укажите один из --source-dir / --source-connection-file").
    """
    if connection:
        return [f"--{label}-connection-file {_quote(str(store.path_for(connection)))}"]
    if dir_:
        return [f"--{label}-dir {_quote(dir_)}"]
    return []


def build_cli_compare(settings: CompareSettings, store: ConnectionStore) -> str:
    parts = ["db-pm compare run", f"--output-dir {_quote(settings.output_dir)}"]
    parts += _side_cli("source", settings.source_connection, settings.source_dir, store)
    parts += _side_cli("target", settings.target_connection, settings.target_dir, store)
    if settings.keep_model_dir:
        parts.append("--keep-model-dir")
    return " ".join(parts)


def build_cli_yaml_generate(settings: YamlGenerateSettings, store: ConnectionStore) -> str:
    del store  # yaml generate does not use connections
    parts = [
        "db-pm yaml generate",
        f"--source {_quote(settings.source_dir)}",
        f"--db-type {settings.db_type}",
        f"--output {_quote(settings.output_file)}",
    ]
    if settings.source_version:
        parts.append(f"--source-version {settings.source_version}")
    return " ".join(parts)


def build_cli_yaml_apply(settings: YamlApplySettings, store: ConnectionStore) -> str:
    del store  # yaml apply does not use connections
    return (
        f"db-pm yaml apply "
        f"--yaml {_quote(settings.yaml_file)} "
        f"--target-db-type {settings.target_db_type} "
        f"--output {_quote(settings.output_dir)}"
    )


def build_cli_deploy_plan(settings: DeployApplySettings, store: ConnectionStore) -> str:
    """Build ``db-pm deploy plan ...`` from GUI settings.

    Mirrors ``presentation/cli/main.py:deploy_plan`` (CLI flags: --include-drops).
    ``confirm_understands_risk`` is intentionally omitted (GUI-side gate, not a
    CLI contract).
    """
    conn_file = store.path_for(settings.target_connection)
    parts = [
        "db-pm deploy plan",
        f"--dir {_quote(settings.codebase_dir)}",
        f"--target-connection-file {_quote(str(conn_file))}",
        f"--output-dir {_quote(settings.output_dir)}",
    ]
    if settings.include_drops:
        parts.append("--include-drops")
    return " ".join(parts)


def build_cli_deploy_apply(settings: DeployApplySettings, store: ConnectionStore) -> str:
    """Build ``db-pm deploy apply ...`` from GUI settings.

    Mirrors ``presentation/cli/main.py:deploy_apply`` (CLI flags: --include-drops,
    --no-rehearsal, --keep-rehearsal-db). ``confirm_understands_risk`` is intentionally
    omitted (GUI-side gate, not a CLI contract).
    """
    conn_file = store.path_for(settings.target_connection)
    parts = [
        "db-pm deploy apply",
        f"--dir {_quote(settings.codebase_dir)}",
        f"--target-connection-file {_quote(str(conn_file))}",
        f"--output-dir {_quote(settings.output_dir)}",
    ]
    if settings.include_drops:
        parts.append("--include-drops")
    if settings.no_rehearsal:
        parts.append("--no-rehearsal")
    if settings.keep_rehearsal_db:
        parts.append("--keep-rehearsal-db")
    return " ".join(parts)


def build_cli_deploy_init_service_schema(
    settings: DeployInitServiceSchemaSettings, store: ConnectionStore
) -> str:
    """Build ``db-pm deploy init-service-schema ...`` from GUI settings.

    Mirrors ``presentation/cli/main.py:deploy_init_service_schema`` (Phase 15.5.2).
    Single required option: --target-connection-file. Idempotent bootstrap of the
    ``__deploy`` service schema (schema + 3 bookkeeping tables) on a target DB.
    """
    conn_file = store.path_for(settings.target_connection)
    return (
        f"db-pm deploy init-service-schema "
        f"--target-connection-file {_quote(str(conn_file))}"
    )

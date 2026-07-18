"""CLI entry point (typer).

Phase 1 surface:
    db-pm reverse-engineer --connection-file <conn.yaml> --output <dir>

Connection management (create/edit) is intentionally UI-only; the CLI consumes
a connection file produced in the GUI (see roadmap §8).
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

import typer

from db_project_manager.application.reverse_engineer import (
    ReverseEngineerError,
    build_default_service,
)
from db_project_manager.infrastructure.config.app_config import load_cfg
from db_project_manager.infrastructure.config.connection_store import (
    ConnectionStore,
    ConnectionStoreError,
)
from db_project_manager.infrastructure.logging_setup import configure as configure_logging

app = typer.Typer(no_args_is_help=True, add_completion=False, help="DB Project Manager CLI.")


@app.callback()
def _main() -> None:
    """DB Project Manager — work with database structure from the command line."""


@app.command("reverse-engineer")
def reverse_engineer(
    connection_file: Annotated[
        Path,
        typer.Option("--connection-file", help="Path to a connection YAML file (created via GUI)."),
    ],
    output: Annotated[
        Optional[Path],
        typer.Option("--output", "-o", help="Output directory. Defaults to config paths.default_output_dir."),
    ] = None,
    config: Annotated[
        Optional[Path],
        typer.Option("--config", help="Path to config.yaml. Defaults to ./config.yaml if present."),
    ] = None,
) -> None:
    """Connect to a database and generate a tree of SQL files from its structure."""
    cfg = load_cfg(config if config is not None else None)
    configure_logging(level=cfg.logging.level, console=True, logs_dir=cfg.paths.logs_dir)

    out_dir = output if output is not None else Path(cfg.paths.default_output_dir)

    store = ConnectionStore()
    try:
        conn_cfg = store.load(connection_file)
    except ConnectionStoreError as e:
        typer.secho(f"Ошибка загрузки подключения: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    def progress(message: str, current: int, total: int) -> None:
        typer.echo(f"[{current}/{total}] {message}")

    service = build_default_service()
    try:
        result = service.run(conn_cfg, out_dir, progress=progress)
    except ReverseEngineerError as e:
        typer.secho(f"Ошибка: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    typer.secho(f"✓ Скрипты сгенерированы в: {result}", fg=typer.colors.GREEN)


if __name__ == "__main__":
    app()

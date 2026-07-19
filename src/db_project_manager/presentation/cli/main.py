"""CLI entry point (typer).

Layout:
    db-pm reverse-engineer --connection-file <conn.yaml> --output <dir>
    db-pm graph     build|export|show|validate  --dir <dir> [...]
    db-pm deploy    validate                    --dir <dir> --connection-file <conn.yaml> [...]

Connection management (create/edit) is UI-only; the CLI consumes a connection
file produced in the GUI (see roadmap §8).
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

import typer

from db_project_manager.application.deploy_service import (
    DeployPermissionError,
    DeployValidateService,
)
from db_project_manager.application.graph_service import BuildGraphService
from db_project_manager.application.reverse_engineer import (
    ReverseEngineerError,
    build_default_service,
)
from db_project_manager.domain.graph import CycleError
from db_project_manager.infrastructure.config.app_config import load_cfg
from db_project_manager.infrastructure.config.connection_store import (
    ConnectionStore,
    ConnectionStoreError,
)
from db_project_manager.infrastructure.graph import graph_store
from db_project_manager.infrastructure.graph.export import export_graph
from db_project_manager.infrastructure.logging_setup import configure as configure_logging

app = typer.Typer(no_args_is_help=True, add_completion=False, help="DB Project Manager CLI.")
graph_app = typer.Typer(no_args_is_help=True, help="Граф зависимостей кодовой базы.")
deploy_app = typer.Typer(no_args_is_help=True, help="Деплой кодовой базы в базу данных.")
app.add_typer(graph_app, name="graph")
app.add_typer(deploy_app, name="deploy")


@app.callback()
def _main() -> None:
    """DB Project Manager — work with database structure from the command line."""


def _load_connection(connection_file: Path) -> object:
    """Load a ConnectionConfig from file or exit with a clear message."""
    store = ConnectionStore()
    try:
        return store.load(connection_file)
    except ConnectionStoreError as e:
        typer.secho(f"Ошибка загрузки подключения: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from e


# --- reverse-engineer (Phase 1) ---


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
    conn_cfg = _load_connection(connection_file)

    def progress(message: str, current: int, total: int) -> None:
        typer.echo(f"[{current}/{total}] {message}")

    service = build_default_service()
    try:
        result = service.run(conn_cfg, out_dir, progress=progress)
    except ReverseEngineerError as e:
        typer.secho(f"Ошибка: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from e

    typer.secho(f"✓ Скрипты сгенерированы в: {result}", fg=typer.colors.GREEN)


# --- graph subapp ---


@graph_app.command("build")
def graph_build(
    directory: Annotated[Path, typer.Option("--dir", help="Codebase root to parse.")],
) -> None:
    """Build the dependency graph and write it to <dir>/.dbm_graph/."""
    configure_logging()
    service = BuildGraphService()
    gdir = service.build_and_store(directory)
    graph = service.build(directory)
    typer.secho(
        f"✓ Граф построен: вершин={len(graph.vertices)}, рёбер={len(graph.edges)}; "
        f"записан в {gdir}",
        fg=typer.colors.GREEN,
    )


@graph_app.command("export")
def graph_export(
    directory: Annotated[Path, typer.Option("--dir", help="Codebase root with .dbm_graph/.")],
    fmt: Annotated[str, typer.Option("--format", help="json|graphml|dot")],
    output: Annotated[
        Optional[Path],
        typer.Option("--output", "-o", help="Output file. Default: <dir>/.dbm_graph/graph.<fmt>"),
    ] = None,
) -> None:
    """Export the stored graph to a standard format."""
    try:
        graph = graph_store.read_graph(directory)
    except graph_store.GraphStoreError as e:
        typer.secho(f"Ошибка: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from e

    out_path = output or (graph_store.graph_dir_for(directory) / f"graph.{fmt.lower()}")
    try:
        export_graph(graph, fmt, out_path)
    except ValueError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from e
    typer.secho(f"✓ Экспорт графа: {out_path}", fg=typer.colors.GREEN)


@graph_app.command("show")
def graph_show(
    directory: Annotated[Path, typer.Option("--dir", help="Codebase root with .dbm_graph/.")],
    object_key: Annotated[str, typer.Option("--object", help="object_key to inspect.")],
) -> None:
    """Show dependencies and dependents of an object."""
    try:
        graph = graph_store.read_graph(directory)
    except graph_store.GraphStoreError as e:
        typer.secho(f"Ошибка: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from e

    if object_key not in graph.vertices:
        typer.secho(f"Объект не найден: {object_key}", fg=typer.colors.YELLOW, err=True)
        raise typer.Exit(code=2)

    deps = graph.get_dependencies(object_key)
    dependents = graph.get_dependents(object_key)
    typer.echo(f"Объект: {object_key}")
    typer.echo(f"Зависимости ({len(deps)}):")
    for e in sorted(deps, key=lambda x: x.destination_object_key):
        typer.echo(f"  -> {e.destination_object_key}  [{e.relation.value}/{e.action}]")
    typer.echo(f"Зависимые ({len(dependents)}):")
    for e in sorted(dependents, key=lambda x: x.source_object_key):
        typer.echo(f"  <- {e.source_object_key}  [{e.relation.value}/{e.action}]")


@graph_app.command("validate")
def graph_validate(
    directory: Annotated[Path, typer.Option("--dir", help="Codebase root with .dbm_graph/.")],
) -> None:
    """Check the stored graph for cycles and dangling references."""
    try:
        graph = graph_store.read_graph(directory)
    except graph_store.GraphStoreError as e:
        typer.secho(f"Ошибка: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from e

    # Cycle check (re-runs toposort which raises on cycles).
    from db_project_manager.infrastructure.graph.topological_sort import topological_sort

    try:
        topological_sort(graph)
    except CycleError as e:
        typer.secho(f"✗ Циклы в графе: {', '.join(sorted(e.unresolved))}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from e

    dangling = graph.dangling_edges()
    if dangling:
        typer.secho(f"✗ Висячие ссылки ({len(dangling)}):", fg=typer.colors.YELLOW)
        for e in dangling[:20]:
            typer.echo(f"  {e.source_object_key} -> {e.destination_object_key}")
        raise typer.Exit(code=1)

    typer.secho("✓ Граф валиден: циклов и висячих ссылок нет.", fg=typer.colors.GREEN)


# --- deploy subapp ---


@deploy_app.command("validate")
def deploy_validate(
    directory: Annotated[Path, typer.Option("--dir", help="Codebase root to deploy.")],
    connection_file: Annotated[Path, typer.Option("--connection-file", help="Server connection YAML.")],
    prefix: Annotated[
        Optional[str],
        typer.Option("--prefix", help="Temp-DB name prefix. Default: codebase dir name."),
    ] = None,
    keep_db: Annotated[bool, typer.Option("--keep-db", help="Keep the temp DB after deploy.")] = False,
    continue_on_error: Annotated[
        bool, typer.Option("--continue-on-error", help="Continue past late-object failures.")
    ] = False,
    config: Annotated[Optional[Path], typer.Option("--config", help="Path to config.yaml.")] = None,
) -> None:
    """Validation deploy: build a temp DB on the server and deploy the codebase into it."""
    cfg = load_cfg(config if config is not None else None)
    configure_logging(level=cfg.logging.level, console=True, logs_dir=cfg.paths.logs_dir)
    conn_cfg = _load_connection(connection_file)

    service = DeployValidateService()

    def progress(message: str, current: int, total: int) -> None:
        if total:
            typer.echo(f"[{current}/{total}] {message}")
        else:
            typer.echo(message)

    try:
        result = service.run(
            conn_cfg,
            directory,
            prefix=prefix,
            keep_db=keep_db,
            continue_on_error=continue_on_error,
            progress=progress,
        )
    except DeployPermissionError as e:
        typer.secho(f"✗ Нет прав: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from e
    except CycleError as e:
        typer.secho(f"✗ Граф содержит циклы: {', '.join(sorted(e.unresolved))}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=3) from e

    if result.success:
        typer.secho(
            f"✓ Деплой успешен: база {result.db_name}, объектов {result.objects_done}/{result.objects_total}",
            fg=typer.colors.GREEN,
        )
    else:
        for err in result.errors:
            typer.secho(
                f"✗ [{err.object_type}] {err.object_name} ({err.source_file}): {err.error}",
                fg=typer.colors.RED,
                err=True,
            )
        typer.secho(
            f" Деплой завершился с ошибками: база {result.db_name}, "
            f"объектов {result.objects_done}/{result.objects_total}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()

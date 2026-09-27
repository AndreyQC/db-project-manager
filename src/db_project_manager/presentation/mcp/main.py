"""db-pm-mcp entry point (Phase 19, MCP-1).

Local MCP server (stdio) over the db-pm connection files. All behavior
policy lives in ``connections/*.yaml`` (``mcp:`` block) — this entry point
takes only bootstrap arguments.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

import typer

app = typer.Typer(add_completion=False, help="DB Project Manager — локальный MCP-сервер (stdio).")


@app.command()
def main(
    connections_dir: Annotated[
        str,
        typer.Option(
            "--connections-dir",
            envvar="DBPM_CONNECTIONS_DIR",
            help="Каталог файлов подключений (connections/*.yaml).",
        ),
    ] = "connections",
    config: Annotated[
        Optional[Path],
        typer.Option("--config", help="Путь к config.yaml (logging, deploy.service_schema)."),
    ] = None,
) -> None:
    """Запустить локальный MCP-сервер db-pm (транспорт stdio).

    Инструменты: list_connections, list_schemas, list_objects,
    get_object_details, query, explain, get_top_queries, run_script,
    deploy_plan, deploy_analyze, deploy_apply, deploy_reset.
    """
    try:
        from db_project_manager.presentation.mcp.server import run_server
    except ImportError as e:
        typer.secho(
            f"MCP SDK не установлен ({e}). Установите extra: uv sync --extra mcp",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2) from e
    run_server(connections_dir=connections_dir, config=config)


if __name__ == "__main__":
    app()

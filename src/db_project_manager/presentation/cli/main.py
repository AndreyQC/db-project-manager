"""CLI entry point (typer).

Layout:
    db-pm reverse-engineer --connection-file <conn.yaml> --output <dir>
    db-pm graph     build|export|show|validate  --dir <dir> [...]
    db-pm deploy    validate                    --dir <dir> --connection-file <conn.yaml> [...]
    db-pm deploy    analyze                     --dir <dir> --target-connection-file <conn.yaml> [...]
    db-pm deploy    plan                        --dir <dir> --target-connection-file <conn.yaml> [...]
    db-pm deploy    apply                       --dir <dir> --target-connection-file <conn.yaml> [...]

Connection management (create/edit) is UI-only; the CLI consumes a connection
file produced in the GUI (see roadmap §8).
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

import typer

from db_project_manager.application.deploy_apply_service import (
    DeployApplyError,
    DeployApplyRejected,
    DeployApplyService,
)
from db_project_manager.application.deploy_service import (
    DeployPermissionError,
    DeployValidateService,
)
from db_project_manager.application.graph_service import BuildGraphService
from db_project_manager.application.safety_gate_service import (
    SafetyGateError,
    SafetyGateService,
)
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
compare_app = typer.Typer(no_args_is_help=True, help="Сравнение состояния БД и кодовой базы.")
yaml_app = typer.Typer(no_args_is_help=True, help="YAML project: generate from directory or apply to target.")
app.add_typer(graph_app, name="graph")
app.add_typer(deploy_app, name="deploy")
app.add_typer(compare_app, name="compare")
app.add_typer(yaml_app, name="yaml")


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


@app.command("qualify-refs")
def qualify_refs(
    directory: Annotated[Path, typer.Option("--dir", help="Codebase root (output of reverse-engineer).")],
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Only scan and report; do not modify files."),
    ] = False,
) -> None:
    """Qualify bare object references in codebase SQL files (Phase 6).

    Walks the codebase, finds bare references to known objects (functions,
    procedures, tables, views) and prefixes them with their schema so the
    generated DDL is deploy-safe regardless of search_path. Writes a
    ``_qualify_report.md`` to the codebase root with the changes and skips.
    """
    configure_logging()
    from db_project_manager.application.qualify_refs_service import (
        QualifyRefsError,
        QualifyRefsService,
    )

    service = QualifyRefsService()
    try:
        report = service.run(directory, dry_run=dry_run)
    except QualifyRefsError as e:
        typer.secho(f"Ошибка: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from e

    mode = "dry-run" if dry_run else "готово"
    typer.secho(
        f"✓ Qualify-refs {mode}: файлов просканировано={report.files_scanned}, "
        f"изменено={len(report.changes)}, ambiguous={len(report.ambiguous)}; "
        f"отчёт: {directory / '_qualify_report.md'}",
        fg=typer.colors.GREEN,
    )


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


# --- compare subapp (Phase 9) ---


def _resolve_side(
    label: str,
    dir_opt: Path | None,
    conn_opt: Path | None,
) -> object:
    """Resolve a comparison side into a SideSpec (DIR or DB).

    Exactly one of ``dir_opt`` / ``conn_opt`` must be set; otherwise exit code 2.
    Returns a :class:`SideSpec` (DB side carries a loaded ConnectionConfig).
    """
    from db_project_manager.application.compare_service import SideSpec
    from db_project_manager.domain.diff import SnapshotSourceKind

    if dir_opt is not None and conn_opt is not None:
        typer.secho(
            f"Укажите ровно один из --{label}-dir / --{label}-connection-file (не оба).",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(code=2)
    if dir_opt is None and conn_opt is None:
        typer.secho(
            f"Укажите один из --{label}-dir или --{label}-connection-file.",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(code=2)

    if dir_opt is not None:
        if not dir_opt.is_dir():
            typer.secho(f"Каталог не существует: {dir_opt}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=2)
        return SideSpec(SnapshotSourceKind.DIR, str(dir_opt))

    # conn_opt is set — load the connection file.
    conn_cfg = _load_connection(conn_opt)
    return SideSpec(SnapshotSourceKind.DB, str(conn_opt), conn_cfg=conn_cfg)


@compare_app.command("run")
def compare_run(
    output_dir: Annotated[Path, typer.Option("--output-dir", help="Каталог для отчётов сравнения.")],
    source_dir: Annotated[Optional[Path], typer.Option("--source-dir", help="Каталог reverse-engineer (source).")] = None,
    source_connection_file: Annotated[
        Optional[Path], typer.Option("--source-connection-file", help="Подключение к БД (source).")
    ] = None,
    target_dir: Annotated[Optional[Path], typer.Option("--target-dir", help="Каталог reverse-engineer (target).")] = None,
    target_connection_file: Annotated[
        Optional[Path], typer.Option("--target-connection-file", help="Подключение к БД (target).")
    ] = None,
    keep_model_dir: Annotated[
        bool, typer.Option("--keep-model-dir", help="Сохранить временный каталог reverse-engineer.")
    ] = False,
    config: Annotated[Optional[Path], typer.Option("--config", help="Путь к config.yaml.")] = None,
) -> None:
    """Сравнить два состояния (БД или каталог reverse-engineer) и записать отчёт."""
    from db_project_manager.application.compare_service import CompareError, CompareService

    load_cfg(config if config is not None else None)
    configure_logging()

    src = _resolve_side("source", source_dir, source_connection_file)
    tgt = _resolve_side("target", target_dir, target_connection_file)

    service = CompareService()

    def progress(message: str, current: int, total: int) -> None:
        if total:
            typer.echo(f"[{current}/{total}] {message}")
        else:
            typer.echo(message)

    try:
        result = service.run(
            src, tgt, output_dir, keep_model_dir=keep_model_dir, progress=progress
        )
    except CompareError as e:
        typer.secho(f"✗ {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from e

    typer.secho(f"✓ Отчёт сравнения: {result}", fg=typer.colors.GREEN)


@compare_app.command("report")
def compare_report(
    from_path: Annotated[Path, typer.Option("--from", help="Путь к diff_report.json.")],
    output: Annotated[
        Optional[Path],
        typer.Option("--output", "-o", help="Куда писать diff_report.md. По умолчанию рядом с --from."),
    ] = None,
) -> None:
    """Сгенерировать markdown-отчёт из готового diff_report.json (офлайн, Phase 14).

    Читает уже существующий ``diff_report.json`` (результат ``db-pm compare run``) и
    пишет читаемый ``diff_report.md`` рядом. Не подключается к БД и не выполняет
    повторное сравнение — работает офлайн.
    """
    from pydantic import ValidationError

    from db_project_manager.infrastructure.diff.markdown_report import write_diff_markdown

    configure_logging()

    if not from_path.is_file():
        typer.secho(f"Файл не найден: {from_path}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    try:
        result = write_diff_markdown(from_path, output)
    except ValidationError as e:
        typer.secho(f"Не удалось разобрать diff_report.json: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from e
    except Exception as e:  # noqa: BLE001 — surface any I/O / parse failure as exit 2
        typer.secho(f"Ошибка генерации отчёта: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from e

    typer.secho(f"✓ Markdown-отчёт: {result}", fg=typer.colors.GREEN)


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


@deploy_app.command("analyze")
def deploy_analyze(
    directory: Annotated[Path, typer.Option("--dir", help="Codebase root to analyze.")],
    target_connection_file: Annotated[
        Path,
        typer.Option(
            "--target-connection-file",
            help="Connection YAML of the EXISTING target DB (must hold data).",
        ),
    ],
    output_dir: Annotated[
        Path,
        typer.Option("--output-dir", help="Where to write safety_gate_report.{md,json}."),
    ],
    config: Annotated[Optional[Path], typer.Option("--config", help="Path to config.yaml.")] = None,
) -> None:
    """Safety gate (dry-run): analyze codebase vs the EXISTING target DB. Read-only.

    Compares the codebase against the live target database, estimates data
    presence for touched tables and matches them with pre-script coverage
    (project.covers). Nothing is applied to the target.

    Exit codes: 0 — clean; 1 — safety-gate violations; 2 — hard error.
    """
    cfg = load_cfg(config if config is not None else None)
    configure_logging(level=cfg.logging.level, console=True, logs_dir=cfg.paths.logs_dir)
    conn_cfg = _load_connection(target_connection_file)

    service = SafetyGateService(service_schema=cfg.deploy.service_schema)

    def progress(message: str, current: int, total: int) -> None:
        if total:
            typer.echo(f"[{current}/{total}] {message}")
        else:
            typer.echo(message)

    try:
        verdict = service.analyze(directory, conn_cfg, output_dir, progress=progress)
    except SafetyGateError as e:
        typer.secho(f"✗ Safety gate: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from e

    md_path = output_dir / "safety_gate_report.md"
    if verdict.clean:
        typer.secho(
            f"✓ Safety gate: CLEAN (тронутых таблиц: {len(verdict.touched)}). "
            f"Отчёт: {md_path}",
            fg=typer.colors.GREEN,
        )
        return

    typer.secho(
        f"✗ Safety gate: VIOLATIONS ({len(verdict.violations)}) — пайплайн остановлен "
        f"(CD-9). Тронутых таблиц: {len(verdict.touched)}. Отчёт: {md_path}",
        fg=typer.colors.RED,
        err=True,
    )
    for violation in verdict.violations:
        typer.secho(
            f"  ! {violation.object_schema}.{violation.name} "
            f"[{violation.touch.value}, ~{violation.estimated_rows} строк] — "
            f"нет покрывающего pre-скрипта",
            fg=typer.colors.RED,
            err=True,
        )
    raise typer.Exit(code=1)


@deploy_app.command("plan")
def deploy_plan(
    directory: Annotated[Path, typer.Option("--dir", help="Codebase root to plan.")],
    target_connection_file: Annotated[
        Path,
        typer.Option(
            "--target-connection-file",
            help="Connection YAML of the EXISTING target DB.",
        ),
    ],
    output_dir: Annotated[
        Path,
        typer.Option("--output-dir", help="Where to write delta/ + plan.{json,md}."),
    ],
    include_drops: Annotated[
        bool,
        typer.Option(
            "--include-drops",
            help="Allow DROP artifacts for REMOVED objects (data tables still blocked).",
        ),
    ] = False,
    config: Annotated[Optional[Path], typer.Option("--config", help="Path to config.yaml.")] = None,
) -> None:
    """Dry-run delta plan: safety gate + ALTER plan + artifacts. Read-only.

    Compares the codebase against the live target database, classifies every
    operation (safe / needs-pre / blocked) and writes the review artifacts:
    delta/NNN_*.sql, plan.json, plan.md. Nothing is applied and no
    pre-scripts are executed.

    Exit codes: 0 — ok; 1 — safety-gate violations; 2 — hard error.
    """
    cfg = load_cfg(config if config is not None else None)
    configure_logging(level=cfg.logging.level, console=True, logs_dir=cfg.paths.logs_dir)
    conn_cfg = _load_connection(target_connection_file)

    service = DeployApplyService(service_schema=cfg.deploy.service_schema)

    def progress(message: str, current: int, total: int) -> None:
        if total:
            typer.echo(f"[{current}/{total}] {message}")
        else:
            typer.echo(message)

    try:
        plan = service.plan(
            directory, conn_cfg, output_dir,
            include_drops=include_drops, progress=progress,
        )
    except DeployApplyRejected as e:
        typer.secho(f"✗ Plan отклонён: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from e
    except DeployApplyError as e:
        typer.secho(f"✗ Plan: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from e

    md_path = output_dir / "plan.md"
    if plan.violations:
        typer.secho(
            f"! План содержит BLOCKED-операции ({len(plan.violations)}) — деплой "
            "невозможен без pre-скриптов/решений. Отчёт: " + str(md_path),
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)
    typer.secho(
        f"✓ План готов: операций {len(plan.operations)} "
        f"(safe: {len(plan.safe_ops)}, needs-pre: {len(plan.needs_pre_ops)}, "
        f"blocked: {len(plan.violations)}). Отчёт: {md_path}",
        fg=typer.colors.GREEN,
    )


@deploy_app.command("apply")
def deploy_apply(
    directory: Annotated[Path, typer.Option("--dir", help="Codebase root to apply.")],
    target_connection_file: Annotated[
        Path,
        typer.Option(
            "--target-connection-file",
            help="Connection YAML of the EXISTING target DB (will be MUTATED).",
        ),
    ],
    output_dir: Annotated[
        Path,
        typer.Option("--output-dir", help="Where to write artifacts (delta/, plan.*, rehearsal/)."),
    ],
    include_drops: Annotated[
        bool,
        typer.Option(
            "--include-drops",
            help="Apply DROP artifacts for REMOVED objects (data tables still blocked).",
        ),
    ] = False,
    no_rehearsal: Annotated[
        bool,
        typer.Option(
            "--no-rehearsal",
            help="Skip the rehearsal phase (CI/throwaway targets only!).",
        ),
    ] = False,
    keep_rehearsal_db: Annotated[
        bool,
        typer.Option(
            "--keep-rehearsal-db",
            help="Keep the rehearsal temp DB after the run (for debugging).",
        ),
    ] = False,
    config: Annotated[Optional[Path], typer.Option("--config", help="Path to config.yaml.")] = None,
) -> None:
    """MUTATES the target DB: rehearse the delta on a temp analog, then apply.

    Full pipeline: safety gate → pre-scripts → re-computed delta (only SAFE
    operations allowed, CD-11) → apply with stop-on-error → post-scripts →
    record schema_version. By default the whole pipeline first runs against a
    rehearsal DB reproducing the target state (seeded from
    __migrations/seed/); a rehearsal failure leaves the target untouched.

    Exit codes: 0 — ok; 1 — safety violations; 2 — hard error.
    """
    cfg = load_cfg(config if config is not None else None)
    configure_logging(level=cfg.logging.level, console=True, logs_dir=cfg.paths.logs_dir)
    conn_cfg = _load_connection(target_connection_file)

    service = DeployApplyService(service_schema=cfg.deploy.service_schema)

    def progress(message: str, current: int, total: int) -> None:
        if total:
            typer.echo(f"[{current}/{total}] {message}")
        else:
            typer.echo(message)

    try:
        result = service.apply(
            directory, conn_cfg, output_dir,
            include_drops=include_drops,
            rehearsal=not no_rehearsal,
            keep_rehearsal_db=keep_rehearsal_db,
            progress=progress,
        )
    except DeployApplyRejected as e:
        typer.secho(f"✗ Apply отклонён: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from e
    except DeployApplyError as e:
        typer.secho(f"✗ Apply: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from e

    rehearsal_note = (
        f", репетиция: {result.rehearsal_db}" if result.rehearsal_db else " (без репетиции)"
    )
    typer.secho(
        f"✓ Apply завершён: применено операций {result.applied}/{result.planned}, "
        f"версия {result.applied_version}{rehearsal_note}. Артефакты: {result.output_dir}",
        fg=typer.colors.GREEN,
    )


# --- yaml subapp (Phase 13) ---


_VALID_DB_TYPES = ("greenplum", "postgres")


@yaml_app.command("generate")
def yaml_generate(
    source: Annotated[
        Path,
        typer.Option("--source", help="Directory with SQL files (reverse-engineer output)."),
    ],
    db_type: Annotated[
        str,
        typer.Option("--db-type", help=f"Source database type: {', '.join(_VALID_DB_TYPES)}."),
    ],
    output: Annotated[
        Path,
        typer.Option("--output", "-o", help="Output YAML file path."),
    ],
    source_version: Annotated[
        str,
        typer.Option("--source-version", help="Optional calver version string (e.g. 2026.08.27.01)."),
    ] = "",
) -> None:
    """Generate a portable YAML project from a directory of SQL files.

    Walks ``--source``, parses all ``*.sql`` files (with or without autodoc
    headers), extracts schema/table/column/function/view definitions, and writes
    a ``.yaml`` file that can later be used to generate a full codebase via
    ``db-pm yaml apply``.

    Exit codes: 0 — ok; 1 — generation error.
    """
    if db_type not in _VALID_DB_TYPES:
        typer.secho(
            f"Invalid --db-type: {db_type!r}. Must be one of: {', '.join(_VALID_DB_TYPES)}.",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(code=2)

    configure_logging()

    try:
        from db_project_manager.infrastructure.yaml_project import (
            YamlGeneratorError,
            generate_yaml_project,
            serialize_yaml_project,
        )
    except ImportError as e:
        typer.secho(f"Import error: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from e

    try:
        project = generate_yaml_project(source, db_type, source_version=source_version)
        yaml_text = serialize_yaml_project(project)
        output.write_text(yaml_text, encoding="utf-8")
    except YamlGeneratorError as e:
        typer.secho(f"Generation error: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from e
    except Exception as e:  # noqa: BLE001
        typer.secho(f"Unexpected error: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from e

    typer.secho(f"Generated YAML: {output}", fg=typer.colors.GREEN)


@yaml_app.command("apply")
def yaml_apply(
    yaml_file: Annotated[
        Path,
        typer.Option("--yaml", help="YAML project file to apply."),
    ],
    target_db_type: Annotated[
        str,
        typer.Option("--target-db-type", help=f"Target database type: {', '.join(_VALID_DB_TYPES)}."),
    ],
    output: Annotated[
        Path,
        typer.Option("--output", "-o", help="Output directory for the generated codebase."),
    ],
) -> None:
    """Generate a full codebase (SQL files + manifest + graph) from a YAML project.

    Parses the YAML file, validates compatibility with ``--target-db-type``,
    generates SQL files using the existing Jinja2 templates (table, view, function,
    external_table), writes a ``dbpm.manifest.json``, and runs ``graph build``.

    For ``greenplum -> postgres``: external tables are skipped (Postgres has no
    writable external tables), ``DISTRIBUTED BY`` / ``WITH (...)`` options are
    dropped. For ``postgres -> greenplum``: an error is raised.

    Exit codes: 0 — ok; 1 — validation / generation error; 2 — target type incompatible.
    """
    if target_db_type not in _VALID_DB_TYPES:
        typer.secho(
            f"Invalid --target-db-type: {target_db_type!r}. Must be one of: {', '.join(_VALID_DB_TYPES)}.",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(code=2)

    configure_logging()

    try:
        from db_project_manager.application.yaml_apply_service import (
            YamlApplyError,
            YamlApplyService,
        )
        from db_project_manager.infrastructure.yaml_project import parse_yaml_project
    except ImportError as e:
        typer.secho(f"Import error: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from e

    try:
        yaml_text = yaml_file.read_text(encoding="utf-8")
        project = parse_yaml_project(yaml_text)
    except Exception as e:  # noqa: BLE001
        typer.secho(f"Failed to parse YAML: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from e

    try:
        service = YamlApplyService()
        result = service.run(project, output, target_db_type)
    except YamlApplyError as e:
        typer.secho(f"Apply error: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from e
    except Exception as e:  # noqa: BLE001
        typer.secho(f"Unexpected error: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from e

    typer.secho(
        f"Applied: schemas={result.schemas_count}, objects={result.objects_count}, "
        f"output={result.output_dir}",
        fg=typer.colors.GREEN,
    )


if __name__ == "__main__":
    app()

r"""Профайлинг всех таблиц схем подключения hrdo_ods (маскированные ODS-данные).

Разовый файл для portable-бандла: кладётся в корень dbpm-portable, запускается
dbpm_profile_hrdo.cmd. ЧТО ПРАВИТЬ ПОД ДРУГИЕ СХЕМЫ/СЕРВЕР:
  - SCHEMAS ниже — единственное место правки под другой набор схем; список
    таблиц снимается с information_schema при каждом запуске (read-only
    каталог), вшитого списка таблиц нет;
  - CONNECTION — имя подключения = имя yaml-файла в connections\ без .yaml;
    хост/креды правятся в том yaml (README-PORTABLE.txt, раздел
    «Другой сервер / другая база»);
  - LIMIT — быстрая проверка: ограничить число таблиц (None = все).

Профайлинг идёт ПО ОДНОЙ таблице с немедленной записью JSON — ошибка/обрыв
на одной таблице не теряет результаты остальных (CLI собирает весь список
в памяти и падает целиком). Пароль: env-ключа ENVOS_CRYPTO_01 на целевой
машине нет — db-pm сам запросит пароль скрытым вводом (README-PORTABLE.txt,
вариант B); кэш пароля живёт в процессе.
"""
import json
import os
import sys

# Тот же bootstrap, что в dbpm_cli.py: pywin32 в --target-layout не
# подхватывается через .pth (embeddable ._pth = isolated mode).
_here = os.path.dirname(os.path.abspath(__file__))
_site = os.path.join(_here, "site")
for _sub in ("win32", os.path.join("win32", "lib"), "pywin32_system32"):
    _p = os.path.join(_site, _sub)
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.append(_p)
_dll = os.path.join(_site, "pywin32_system32")
if os.path.isdir(_dll):
    os.add_dll_directory(_dll)
    os.environ["PATH"] = _dll + os.pathsep + os.environ.get("PATH", "")

# Импорты — только после pywin32-bootstrap выше (path/DLL должны быть
# прописаны до загрузки пакета), поэтому E402 здесь — осознанно.
from pathlib import Path  # noqa: E402

import typer  # noqa: E402

from db_project_manager.application.profiling_service import ProfilingError, ProfilingService  # noqa: E402
from db_project_manager.domain.connection import ConnectionConfig  # noqa: E402
from db_project_manager.infrastructure.database.base import DatabaseAdapter  # noqa: E402
from db_project_manager.infrastructure.profiling.base import quote_ident, quote_literal  # noqa: E402
from db_project_manager.presentation.cli.main import (  # noqa: E402
    _make_cli_password_prompt,
    _safe_path_part,
)

CONNECTION = "IVSD00258.reksoft.com_PG__DB__hrdo_ods__U_postgres"

# Схемы для профайлинга — правится под другой набор схем (mixed-case схем
# тоже допустим: кавычки ставятся автоматически).
SCHEMAS = ["ods_hn_zup_masked", "ods_hn_trade_zup_masked"]

# Быстрая проверка: ограничить число таблиц (None = профилировать все).
LIMIT: int | None = None


def _echo(text: str, err: bool = False, red: bool = False) -> None:
    if red:
        typer.secho(text, fg=typer.colors.RED, err=err)
    else:
        typer.echo(text, err=err)


def _discover_tables(
    service: ProfilingService, cfg: ConnectionConfig, adapter: DatabaseAdapter, timeout_s: int
) -> list[str]:
    """BASE TABLE по SCHEMAS из information_schema; имена — "schema"."table"."""
    schema_list = ", ".join(quote_literal(s) for s in SCHEMAS)
    sql = (
        "SELECT table_schema, table_name FROM information_schema.tables\n"
        f"WHERE table_type = 'BASE TABLE' AND table_schema IN ({schema_list})\n"
        "ORDER BY 1, 2"
    )
    result = service._run(adapter, cfg, sql, "каталог таблиц схем", max_rows=100000, timeout_s=timeout_s)
    return [f"{quote_ident(r['table_schema'])}.{quote_ident(r['table_name'])}" for r in result.rows]


def main() -> None:
    service = ProfilingService(
        connections_dir=str(Path(_here) / "connections"),
        password_prompt=_make_cli_password_prompt(),
    )
    out_dir = Path(_here) / "reports" / _safe_path_part(CONNECTION)
    out_dir.mkdir(parents=True, exist_ok=True)

    cfg = service._manager.load_config(CONNECTION)
    if not cfg.profiling_settings.enabled:
        _echo("Ошибка: в yaml подключения нет блока profiling: enabled=true", err=True, red=True)
        sys.exit(2)
    timeout_s = max(1, cfg.profiling_settings.statement_timeout_ms // 1000)

    with service._manager.connection(CONNECTION) as managed:
        tables = _discover_tables(service, cfg, managed.adapter, timeout_s)
    if not tables:
        _echo(
            f"Ошибка: в схемах {', '.join(SCHEMAS)} не найдено ни одной таблицы (BASE TABLE) — "
            "проверьте SCHEMAS в этом файле и содержимое базы",
            err=True,
            red=True,
        )
        sys.exit(2)
    _echo(f"Схемы: {', '.join(SCHEMAS)}; найдено таблиц: {len(tables)}")
    if LIMIT is not None:
        tables = tables[:LIMIT]
        _echo(f"LIMIT={LIMIT}: профилируются первые {len(tables)}")

    done, failed = 0, []
    for raw in tables:
        try:
            profiles = service.profile_tables(CONNECTION, [raw])
        except ProfilingError as exc:
            _echo(f"ОШИБКА {raw}: {exc} — иду к следующей таблице", err=True, red=True)
            failed.append(raw)
            continue
        for tp in profiles:
            target = out_dir / f"{_safe_path_part(tp.schema_name)}.{_safe_path_part(tp.table_name)}.json"
            target.write_text(json.dumps(tp.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8")
            size = f"{tp.size_bytes / 1048576:.1f} MB" if tp.size_bytes else "n/a"
            mode = "sampled" if tp.sampled else "full"
            _echo(f"OK {tp.schema_name}.{tp.table_name}: rows={tp.row_count}, size={size}, {mode}")
        done += 1

    _echo(f"Готово: {done}/{len(tables)} профилей в {out_dir}")
    if failed:
        _echo(f"Не удалось ({len(failed)}): {', '.join(failed)}", err=True, red=True)
        _echo(
            "Перезапуск точечной таблицы: dbpm.cmd profile " + CONNECTION + ' --tables "\\"schema\\".\\"Table\\""',
            err=True,
        )
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except typer.Exit as exc:
        sys.exit(exc.exit_code)
    except KeyboardInterrupt:
        _echo("Прервано пользователем; готовые JSON сохранены, перезапуск продолжит/перезапишет", err=True)
        sys.exit(130)

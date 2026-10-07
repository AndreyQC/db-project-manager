"""Профайлинг всех таблиц подключения hrdo_ods (маскированные ODS-схемы).

Разовый файл для portable-бандла: кладётся в корень dbpm-portable, запускается
dbpm_profile_hrdo.cmd. Список таблиц вшит — снимок 2026-10-07 с бэкапа
от 2026-10-06 (данные статичны): 62 таблицы в ods_hn_trade_zup_masked,
57 в ods_hn_zup_masked. Имена mixed-case, поэтому каждое — в двойных кавычках
("schema"."Table"); парсер Phase 20 снимает кавычки и сохраняет регистр.

Отличие от CLI `db-pm profile --tables ...`: профайлинг идёт ПО ОДНОЙ таблице
с немедленной записью JSON — ошибка/обрыв на одной таблице не теряет результаты
остальных (CLI собирает весь список в памяти и падает целиком). Пароль:
env-ключа ENVOS_CRYPTO_01 на целевой машине нет — db-pm сам запросит пароль
скрытым вводом (README-PORTABLE.txt, вариант B); кэш пароля живёт в процессе.
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

from db_project_manager.application.profiling_service import (  # noqa: E402
    ProfilingError,
    ProfilingService,
)
from db_project_manager.presentation.cli.main import (  # noqa: E402
    _make_cli_password_prompt,
    _safe_path_part,
)

CONNECTION = "IVSD00258.reksoft.com_PG__DB__hrdo_ods__U_postgres"

TABLES = [
    '"ods_hn_trade_zup_masked"."Account_groups"',
    '"ods_hn_trade_zup_masked"."Accounting_for_employee_salaries"',
    '"ods_hn_trade_zup_masked"."Accounting_for_employee_salaries_period"',
    '"ods_hn_trade_zup_masked"."Application_of_planned_accruals"',
    '"ods_hn_trade_zup_masked"."BusinessStream"',
    '"ods_hn_trade_zup_masked"."Contract_basis_for_concluding"',
    '"ods_hn_trade_zup_masked"."Contract_information_for_employee"',
    '"ods_hn_trade_zup_masked"."Cost_centers"',
    '"ods_hn_trade_zup_masked"."Counterparty"',
    '"ods_hn_trade_zup_masked"."Department"',
    '"ods_hn_trade_zup_masked"."Dept"',
    '"ods_hn_trade_zup_masked"."Dept_1"',
    '"ods_hn_trade_zup_masked"."Employee"',
    '"ods_hn_trade_zup_masked"."FIOFizicheskikhLits"',
    '"ods_hn_trade_zup_masked"."Food_cards_employees"',
    '"ods_hn_trade_zup_masked"."Function"',
    '"ods_hn_trade_zup_masked"."Grade_categories"',
    '"ods_hn_trade_zup_masked"."Grades"',
    '"ods_hn_trade_zup_masked"."Grades_employee"',
    '"ods_hn_trade_zup_masked"."HRBP"',
    '"ods_hn_trade_zup_masked"."HRBP_1"',
    '"ods_hn_trade_zup_masked"."HRadmin"',
    '"ods_hn_trade_zup_masked"."Health_insurance_program"',
    '"ods_hn_trade_zup_masked"."Health_insurance_program_employee"',
    '"ods_hn_trade_zup_masked"."History_of_employees"',
    '"ods_hn_trade_zup_masked"."History_of_employees_period"',
    '"ods_hn_trade_zup_masked"."History_of_food_card_limits"',
    '"ods_hn_trade_zup_masked"."History_of_manage_employees"',
    '"ods_hn_trade_zup_masked"."History_of_manage_employees_period"',
    '"ods_hn_trade_zup_masked"."Insurance_person"',
    '"ods_hn_trade_zup_masked"."KadrovayaIstoriyaSotrudnikov"',
    '"ods_hn_trade_zup_masked"."Limits_on_food_cards"',
    '"ods_hn_trade_zup_masked"."Location"',
    '"ods_hn_trade_zup_masked"."ManagementUnit"',
    '"ods_hn_trade_zup_masked"."Manager_for_employee"',
    '"ods_hn_trade_zup_masked"."Meal_cards_issued_to_employees"',
    '"ods_hn_trade_zup_masked"."Perscent_northern_allowance_persons"',
    '"ods_hn_trade_zup_masked"."Person"',
    '"ods_hn_trade_zup_masked"."PersonalData"',
    '"ods_hn_trade_zup_masked"."Plan_accrual"',
    '"ods_hn_trade_zup_masked"."Plan_accrual_interval"',
    '"ods_hn_trade_zup_masked"."Plan_of_calculation_types"',
    '"ods_hn_trade_zup_masked"."Position_Staff"',
    '"ods_hn_trade_zup_masked"."Position_rus"',
    '"ods_hn_trade_zup_masked"."Positions"',
    '"ods_hn_trade_zup_masked"."Production_calendar_monthliy"',
    '"ods_hn_trade_zup_masked"."Production_calendars"',
    '"ods_hn_trade_zup_masked"."Reason_for_absence"',
    '"ods_hn_trade_zup_masked"."Region"',
    '"ods_hn_trade_zup_masked"."Report_groups_TS"',
    '"ods_hn_trade_zup_masked"."Salary_calculation_metrics"',
    '"ods_hn_trade_zup_masked"."Status_data_employees"',
    '"ods_hn_trade_zup_masked"."Status_excep_employees"',
    '"ods_hn_trade_zup_masked"."Stream"',
    '"ods_hn_trade_zup_masked"."SubLocation"',
    '"ods_hn_trade_zup_masked"."Types_of_employment"',
    '"ods_hn_trade_zup_masked"."Types_of_employment_period"',
    '"ods_hn_trade_zup_masked"."Types_of_reception"',
    '"ods_hn_trade_zup_masked"."Types_of_time"',
    '"ods_hn_trade_zup_masked"."Values_of_periodic_metrics_employee_int"',
    '"ods_hn_trade_zup_masked"."key_fields"',
    '"ods_hn_trade_zup_masked"."test1"',
    '"ods_hn_zup_masked"."Account_groups"',
    '"ods_hn_zup_masked"."Accounting_for_employee_salaries"',
    '"ods_hn_zup_masked"."Accounting_for_employee_salaries_period"',
    '"ods_hn_zup_masked"."Application_of_planned_accruals"',
    '"ods_hn_zup_masked"."BusinessStream"',
    '"ods_hn_zup_masked"."Contract_basis_for_concluding"',
    '"ods_hn_zup_masked"."Contract_information_for_employee"',
    '"ods_hn_zup_masked"."Cost_centers"',
    '"ods_hn_zup_masked"."Counterparty"',
    '"ods_hn_zup_masked"."Department"',
    '"ods_hn_zup_masked"."Dept"',
    '"ods_hn_zup_masked"."Employee"',
    '"ods_hn_zup_masked"."Food_cards_employees"',
    '"ods_hn_zup_masked"."Function"',
    '"ods_hn_zup_masked"."Grade_categories"',
    '"ods_hn_zup_masked"."Grades"',
    '"ods_hn_zup_masked"."Grades_employee"',
    '"ods_hn_zup_masked"."HRBP"',
    '"ods_hn_zup_masked"."HRadmin"',
    '"ods_hn_zup_masked"."Health_insurance_program"',
    '"ods_hn_zup_masked"."Health_insurance_program_employee"',
    '"ods_hn_zup_masked"."History_of_employees"',
    '"ods_hn_zup_masked"."History_of_employees_period"',
    '"ods_hn_zup_masked"."History_of_food_card_limits"',
    '"ods_hn_zup_masked"."History_of_manage_employees"',
    '"ods_hn_zup_masked"."History_of_manage_employees_period"',
    '"ods_hn_zup_masked"."Insurance_person"',
    '"ods_hn_zup_masked"."Limits_on_food_cards"',
    '"ods_hn_zup_masked"."Location"',
    '"ods_hn_zup_masked"."ManagementUnit"',
    '"ods_hn_zup_masked"."Manager_for_employee"',
    '"ods_hn_zup_masked"."Meal_cards_issued_to_employees"',
    '"ods_hn_zup_masked"."Perscent_northern_allowance_persons"',
    '"ods_hn_zup_masked"."Person"',
    '"ods_hn_zup_masked"."PersonalData"',
    '"ods_hn_zup_masked"."Plan_accrual"',
    '"ods_hn_zup_masked"."Plan_accrual_interval"',
    '"ods_hn_zup_masked"."Plan_of_calculation_types"',
    '"ods_hn_zup_masked"."Position_Staff"',
    '"ods_hn_zup_masked"."Position_rus"',
    '"ods_hn_zup_masked"."Positions"',
    '"ods_hn_zup_masked"."Production_calendar_monthliy"',
    '"ods_hn_zup_masked"."Production_calendars"',
    '"ods_hn_zup_masked"."Reason_for_absence"',
    '"ods_hn_zup_masked"."Region"',
    '"ods_hn_zup_masked"."Report_groups_TS"',
    '"ods_hn_zup_masked"."Salary_calculation_metrics"',
    '"ods_hn_zup_masked"."Status_data_employees"',
    '"ods_hn_zup_masked"."Status_excep_employees"',
    '"ods_hn_zup_masked"."Stream"',
    '"ods_hn_zup_masked"."SubLocation"',
    '"ods_hn_zup_masked"."Types_of_employment"',
    '"ods_hn_zup_masked"."Types_of_employment_period"',
    '"ods_hn_zup_masked"."Types_of_reception"',
    '"ods_hn_zup_masked"."Types_of_time"',
    '"ods_hn_zup_masked"."Values_of_periodic_metrics_employee_int"',
    '"ods_hn_zup_masked"."key_fields"',
]


def _echo(text: str, err: bool = False, red: bool = False) -> None:
    if red:
        typer.secho(text, fg=typer.colors.RED, err=err)
    else:
        typer.echo(text, err=err)


def main() -> None:
    service = ProfilingService(
        connections_dir=str(Path(_here) / "connections"),
        password_prompt=_make_cli_password_prompt(),
    )
    out_dir = Path(_here) / "reports" / _safe_path_part(CONNECTION)
    out_dir.mkdir(parents=True, exist_ok=True)

    done, failed = 0, []
    for raw in TABLES:
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

    _echo(f"Готово: {done}/{len(TABLES)} профилей в {out_dir}")
    if failed:
        _echo(f"Не удалось ({len(failed)}): {', '.join(failed)}", err=True, red=True)
        _echo("Перезапуск точечной таблицы: dbpm.cmd profile " + CONNECTION + ' --tables "\"schema\".\"Table\""', err=True)
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except typer.Exit as exc:
        sys.exit(exc.exit_code)
    except KeyboardInterrupt:
        _echo("Прервано пользователем; готовые JSON сохранены, перезапуск продолжит/перезапишет", err=True)
        sys.exit(130)

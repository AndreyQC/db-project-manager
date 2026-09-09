# Phase 15.8 (result): yaml apply — конвертация external tables в обычные

> Контекст:
> - `_docs_/_tasks_/phase_15/Phase_15.8_final.md` — план с закрытыми решениями Р-1..Р-4
> - `_docs_/_tasks_/phase_15/Phase_15.8_draft.md` — драфт с обсуждением (история)
> - `LESSONS_LEARNED.md` §53–§58 — базовые уроки Phase 13

**Дата:** 2026-09-08
**Статус:** реализовано, тесты зелёные

---

## Что сделано

`db-pm yaml apply` (CLI + GUI) получил режим «конвертировать внешние таблицы в
обышие»: external_tables из YAML становятся обычными таблицами — колонки и имя
1:1, `location` / `format_type` / `format_options` / `encoding` отбрасываются,
в SQL остаётся комментарий-провенанс. Реализованы все решения Р-1..Р-4:

| Решение | Реализация |
|---|---|
| Р-1: явный `DISTRIBUTED RANDOMLY` при пустом `distributed_by` (greenplum-таргет, для ВСЕХ таблиц) | `_write_table_sql`: else-ветка у `if table.distributed_by:` |
| Р-2: имя 1:1 | `_convert_external_tables` переносит `ext.name` без изменений |
| Р-3: флаг снимает запрет GP→postgres | конвертация выполняется ДО `_validate` — external-списки пусты, запрет не срабатывает |
| Р-4: провенанс-комментарий | `-- converted from external table; source LOCATION: ...; FORMAT: ...` первой строкой sql_body |

## Изменённые файлы

| Файл | Изменение |
|---|---|
| `src/db_project_manager/application/yaml_apply_service.py` | `YamlApplyResult.converted_external_tables`; `run(..., convert_external_to_tables=False)`; `_convert_external_tables` (чистая пересборка pydantic-моделей + guard коллизий имён + provenance-map); Р-1 else-ветка; провенанс в `_write_table_sql` |
| `src/db_project_manager/presentation/cli/main.py` | флаг `--convert-external-to-tables`; вывод `converted_external=N` |
| `src/db_project_manager/presentation/gui/actions/models.py` | `YamlApplySettings.convert_external_to_tables: bool = False` |
| `src/db_project_manager/presentation/gui/actions/dialogs.py` | чекбокс «Конвертировать внешние таблицы в обычные» с tooltip |
| `src/db_project_manager/presentation/gui/actions/cli.py` | `build_cli_yaml_apply` добавляет флаг при True |
| `src/db_project_manager/presentation/gui/actions/registry.py` | проброс флага в `YamlApplyWorker` |
| `src/db_project_manager/presentation/gui/widgets/workers.py` | параметр `convert_external_to_tables` → `service.run(...)`; статус-строка со счётчиком |
| `tests/unit/test_yaml_project.py` | класс `TestConvertExternalTables` (6 тестов); обновлён assert t2 в `TestApplyLayout` под Р-1 |
| `tests/unit/test_action_cli.py` | 3 string-теста билдера + CliRunner-контракт (флаг доходит до сервиса) |
| `README.md` | секция `yaml generate` / `yaml apply` в CLI-документации (ранее отсутствовала) + описание флага |

## Проверки

```bash
# Окружение: Device Guard обход (LESSONS §68)
PYTHONPATH="src;.venv/Lib/site-packages" ~/AppData/Roaming/uv/python/cpython-3.13.*/python.exe \
    -m pytest tests/unit/ -q -p no:randomly
# 965 passed (базово 955 + 10 новых Phase 15.8)

.venv/Scripts/ruff.exe check src/ tests/
# All checks passed!
```

Новые тесты покрывают: конвертацию на greenplum (раскладка `tables/table x.sql`,
autodoc `object_type: table`, отсутствие LOCATION/FORMAT, провенанс, RANDOMLY,
колонки 1:1), конвертацию на postgres без ошибки (Р-3), регрессии без флага
(ошибка GP→PG сохраняется, external пишется как раньше), счётчики, коллизию
имён, расширенное Р-1 для обычных таблиц, GUI-билдер и CLI-контракт.

## Известные ограничения

- **Behavior change (Р-1, осознанный):** таблицы с пустым `distributed_by`
  при greenplum-таргете теперь получают явный `DISTRIBUTED RANDOMLY`; ранее
  clause не писался (GP default — hash по первой колонке). Обновлённый assert
  t2 фиксирует новый контракт.
- Провенанс-комментарий попадает в БД при деплое (безвредный SQL-комментарий).
- Roundtrip `yaml generate` → `yaml apply --convert...`: тип объекта меняется
  один раз; повторная конвертация уже нечего конвертировать.
- `yaml generate` флагом не затрагивается.

## NOT done (в рамках фазы)

- Обратная конвертация (table → external), загрузка данных из внешних источников.
- Эвристики синтеза `distributed_by` / `with_options`.
- Integration-тесты конвертации на живом GP (unit-покрытие достаточно: сервис
  не работает с БД).

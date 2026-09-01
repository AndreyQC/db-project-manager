# Phase 13: GP↔PG YAML Pipeline

> **Дата:** 2026-08-28
> **Ветка:** dev
> **Статус:** завершена

## Контекст

Нужен portable формат для представления структуры базы данных (схемы → таблицы/представления/функции/external tables с колонками и типами), пригодный для:
- хранения в git как human-readable артефакт
- трансформации Greenplum → PostgreSQL (external tables пропускаются, DISTRIBUTED BY / WITH дропаются)
- генерации codebase из YAML (SQL файлы + dbpm.manifest.json + .dbm_graph/)

CD-17..19 (отчёты, post-scripts, параметры) отложены в Phase 15.

## Архитектура

### YAML-схема (`domain/yaml_project.py`)

```yaml
db_type: greenplum
database: cis_zup
generated_at: 2026-08-28T...
schemas:
  - schema_name: cis_dmt_zup
    tables:
      - table_name: lu_zup_costcenters
        columns:
          - column_name: cost_center_code; type: text; nullable: true
          - column_name: cost_centers_guid; type: text; nullable: false
        distributed_by: []           # GP only; [] = DISTRIBUTED RANDOMLY
        with_options: {appendoptimized: TRUE, orientation: COLUMN, ...}  # GP only
    views: [...]
    functions: [...]
  - schema_name: ch_cis_zup
    external_tables:
      - external_table_name: ext_w_staging_tr_zup_headcount_calc_result
        location: "pxf://...?PROFILE=JDBC&..."
        format_type: CUSTOM
        format_options: "(FORMATTER='pxfwritable_export')"
        encoding: UTF8
        columns: [...]   # column_name; nullable=True для всех external table колонок
```

Ключевые решения:
- `db_type` —Literal驱动 validation; GP-специфичные поля (`distributed_by`, `with_options`) присутствуют для greenplum, отсутствуют для postgres
- имена сущностей сериализуются описательными ключами (`schema_name`/
  `table_name`/`column_name`/`view_name`/`function_name`/`external_table_name`),
  не общим `name` на всех уровнях (feedback 2026-08-31: в длинном YAML общий
  `name` затрудняет поиск ошибок); Python-атрибут остаётся `name`, чтение
  принимает и legacy `name` (AliasChoices) — старые файлы парсятся
- `definition` для views/functions — сырой SQL body, не re-normalized
- `columns` для external tables — всегда `nullable=True` (GP semantics)
- Identity key без catalog-сегмента (`database/<db>/schema/<s>/type/<t>/name/<n>`)

### Парсеры колонок по типам БД (`infrastructure/yaml_project/column_parsers.py`)

Единая точка входа `parse_columns(sql_body, db_type)`:

| db_type | Парсер | Примечание |
|---------|--------|-----------|
| greenplum | regex (depth-tracking) | WITH/DISTRIBUTED BY, comma-first style |
| postgres | sqlglot | полная поддержка |
| clickhouse | sqlglot + skip MATERIALIZED/EPHEMERAL/ALIAS | |
| snowflake, mssql, sqlite, oracle | sqlglot | |
| <unknown> | sqlglot postgres fallback | graceful degradation |

Depth-tracking parenthesis counter корректно обрабатывает:
- `NUMERIC(10,2)` — nested parens в типах
- `DEFAULT (CURRENT_TIMESTAMP)` — DEFAULT expressions с parens
- `LOCATION(...)` на той же строке что и closing `)` column list
- Trailing/leading commas: `col TYPE,` и `,col TYPE`

### Feedback-фиксы (31.08, реальная база cis_zup, коммит `e5a6f50`)

- Детект external table: clause-regex `\bLOCATION\s*\(` вместо подстроки
  `"LOCATION" in upper` — колонки `location_guid`/`sublocation_name` превращали
  17 обычных таблиц в external (потеря NOT NULL / distributed_by / with_options).
  Проверено на корпусе: 106/87 → 123 tables / 70 external, 0 ext с пустым location.
- `_RE_FORMAT`: внешний слой скобок опций снимается, `format_options`
  хранится без них (шаблон `external_table.sql.j2` добавляет скобки сам);
  ранее lookahead `\)` обрезал закрывающую скобку.
- Типы колонок нормализуются в `YamlColumn` (единая точка): lowercase,
  пробелы вокруг скобок/запятых схлопываются — `NUMERIC (38, 0)` → `numeric(38,0)`.
- Сериализация `sort_keys=False`: ключ имени (`schema_name`, `table_name`,
  `column_name`, ...) — первый в блоке; алфавитный порядок прятал имя под
  сотнями строк колонок. Уроки — LESSONS §53-54.

### Feedback-фиксы yaml apply (31.08, коммит `d5ab2cb`)

- Раскладка вывода приведена к конвенции RE `<schema>/<kind>/<type> <name>.sql`
  без вложенных подкаталогов: `external_tables/external_table x.sql`
  (было `external_tables/ext/ext x.sql`), аналогично tables/views/
  materialized_views/functions.
- `_inject_gp_options`: инъекция через `rfind(");")`, `WITH (...)` и
  `DISTRIBUTED BY (...)` — отдельные клавизы; прежний `replace(");", ...)`
  портил SQL (съедена закрывающая скобка, DISTRIBUTED BY внутри WITH).
- `with_options` рендерятся независимо от `distributed_by` (раньше тиходроп
  при DISTRIBUTED RANDOMLY — 123 таблицы на cis_zup).
- `DEFAULT` колонок сохраняется (раньше `_default_mod` возвращал пустую
  строку — потеря 244 выражений на cis_zup).
- Инвариант: roundtrip `generate -> apply -> generate` на cis_zup даёт
  0 расхождений по всем полям всех 277 объектов. Урок — LESSONS §55.

### Feedback-фиксы yaml generate (01.09, коммит `89b6a7a`)

- Краш `exp.View` (sqlglot 27): tree-парсеры view/function переписаны по
  фактическому AST (Create.kind + MaterializedProperty/ReturnsProperty/
  LanguageProperty/SecurityProperty) — продолжение урока §44.
- Схема объектов из файлов без autodoc берётся из qualified имени DDL
  (`ParsedSqlObject.schema`; sqlglot `Table.db` + GP regex-захват); было —
  всегда `public`.
- Три класса входа с разными сообщениями: без autodoc (fallback + совет),
  сломанный autodoc (identity salvage построчно, пересборка in-memory),
  валидный. Флаги: `--require-autodoc` (strict/CI), `--fix-broken-autodoc`
  (перезапись сломанных заголовков на диск; remarks-секции удаляются с
  отчётом). Уроки — LESSONS §56.
- Корпус cis_zup (PG): 277 сломанных заголовков (миграционный скрипт писал
  `': '` без кавычек) восстановлены salvage; объекты сходятся с GP-стороной.

### extract_columns: DROP+CREATE (01.09, коммит `ff0eb9f`)

- `parse_one` брал первый стейтмент тела; RE-файлы начинаются с
  `DROP TABLE IF EXISTS ... CASCADE;` -> None -> 0 колонок во всех таблицах
  PG-корпуса (GP-путь маскировался regex-парсером). Теперь CREATE ищется
  среди всех стейтментов (`sqlglot.parse`). Латентно то же влияло на
  колоночный diff Phase 12. Урок — LESSONS §57.
- Проверка: GP vs PG YAML — 193 объекта, 0 различий наборов колонок
  (2025 колонок в PG-стороне).

### yaml apply: definition verbatim (01.09, коммит `afdeddd`)

- View-файлы получали невалидный двойной CREATE: шаблон `view.sql.j2`
  оборачивал definition в свой `CREATE OR REPLACE VIEW ... AS`, хотя
  definition по контракту — полное тело стейтмента. Теперь view/function
  writer'ы пишут definition дословно (autodoc + тело без обёрток); шаблоны
  остаются для RE-пути.
- Уточнение к roundtrip-инварианту выше: проверка 31.08 покрывала только
  таблицы (скрипт сравнения потерял views/functions при рефакторинге —
  LESSONS §58). Полное покрытие с assert'ом счётчиков по типам:
  193 tables / 11 views / 67 functions — 0 расхождений по всем полям.

### Компоненты

| Файл | Назначение |
|------|-----------|
| `domain/yaml_project.py` | Pydantic-модели: YamlProject, YamlSchema, YamlTable, YamlExternalTable, YamlView, YamlFunction, YamlColumn |
| `infrastructure/yaml_project/column_parsers.py` | Per-db-type column extraction |
| `infrastructure/yaml_project/autodoc_parser.py` | parse_autodoc_object + parse_sql_object; autodoc routing + GP DDL regex fallback |
| `infrastructure/yaml_project/serializer.py` | serialize_yaml_project / parse_yaml_project |
| `infrastructure/yaml_project/generator.py` | generate_yaml_project (directory walk → YamlProject) |
| `infrastructure/yaml_project/__init__.py` | Exports |
| `infrastructure/templates/external_table.sql.j2` | Jinja2 шаблон для GP writable external tables |
| `application/yaml_apply_service.py` | YamlApplyService.run: YAML → SQL + manifest + graph |
| `presentation/cli/main.py` | `db-pm yaml generate` / `yaml apply` |
| `presentation/gui/actions/models.py` | YamlGenerateSettings, YamlApplySettings |
| `presentation/gui/actions/cli.py` | build_cli_yaml_generate, build_cli_yaml_apply |
| `presentation/gui/actions/dialogs.py` | YamlGenerateDialog, YamlApplyDialog |
| `presentation/gui/actions/registry.py` | ActionSpec entries для yaml_generate / yaml_apply |
| `presentation/gui/widgets/workers.py` | YamlGenerateWorker, YamlApplyWorker |
| `tests/unit/test_yaml_project.py` | 8 unit-тестов Phase 13 |

### CLI

```bash
# Greenplum directory → YAML
db-pm yaml generate --source <dir> --db-type greenplum --output project.yaml [--source-version 2026.08.28.01]

# YAML → codebase (GP или PG)
db-pm yaml apply --yaml project.yaml --target-db-type postgres --output codebase/
```

GP→PG трансформации:
- external tables пропускаются (Postgres не имеет writable external tables)
- `distributed_by` / `with_options` дропаются из шаблонов
- Warning в лог при пропуске external tables

## Проверки

```bash
uv run ruff check src/ tests/      # All checks passed
uv run pytest tests/unit/ -q       # 854 passed
```

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
  - name: cis_dmt_zup
    tables:
      - name: lu_zup_costcenters
        columns:
          - name: cost_center_code; type: text; nullable: true
          - name: cost_centers_guid; type: text; nullable: false
        distributed_by: []           # GP only; [] = DISTRIBUTED RANDOMLY
        with_options: {appendoptimized: TRUE, orientation: COLUMN, ...}  # GP only
    views: [...]
    functions: [...]
  - name: ch_cis_zup
    external_tables:
      - name: ext_w_staging_tr_zup_headcount_calc_result
        location: "pxf://...?PROFILE=JDBC&..."
        format_type: CUSTOM
        format_options: "(FORMATTER='pxfwritable_export')"
        encoding: UTF8
        columns: [...]   # nullable=True для всех external table колонок
```

Ключевые решения:
- `db_type` —Literal驱动 validation; GP-специфичные поля (`distributed_by`, `with_options`) присутствуют для greenplum, отсутствуют для postgres
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

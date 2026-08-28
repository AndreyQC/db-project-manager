## Phase 13 — GP↔PG YAML Pipeline

**Резюме:** две CLI-команды — `db-pm yaml generate` ( Greenplum/Postgres directory → portable YAML) и `db-pm yaml apply` (YAML → codebase dir с manifest + `_deploy`). Параллельно CD-16..19 откладываются в Phase 15.

---

### Схема YAML (domain-слой, `domain/yaml_project.py`)

```yaml
# Portable project YAML — топоуровневый источник правды
db_type: greenplum                    # greenplum | postgres
database: cis_zup
generated_at: 2026-08-27T...
source_version: 2026.08.27.01

schemas:
  - name: cis_dmt_zup
    tables:
      - name: lu_zup_accountgroups
        columns:
          - name: account_groups_code; type: text; nullable: true; default: null
          - name: account_groups_guid; type: text; nullable: false; default: null
          ...
        distributed_by: [account_groups_guid]    # GP only
        with_options: { appendoptimized: true, orientation: column }  # GP only
    views:
      - name: v_tr_zup_...
        definition: "SELECT ... FROM ..."
    functions:
      - name: fun_lu_zup_accountgroups_etl
        returns: json
        arguments: [{ name: p_parameters_json, type: json }]
        definition: "..."          # полный SQLBODY
    ...
  - name: ch_cis_zup
    external_tables:
      - name: ext_w_staging_tr_zup_headcount_calc_result
        location: "...pxf://..."
        format: { type: custom, formatter: pxfwritable_export }
        columns: [...]
    ...
```

**Ключевые решения по схеме:**
- `db_type` — определяет, какие поля ожидать (`distributed_by`, `with_options` для GP; отсутствуют для PG)
- `definition` для views/functions — сырой SQL-текст (sqlglot normalize), автодок не хранится
- Колонки для всех типов (table, external_table, view) — из SQL-body или autodoc
- Identity ключ — `object_key` формат `database/<db>/schema/<s>/type/<t>/name/<n>` (без catalog-сегмента для совместимости между клонами, §51)

**Новые файлы:**
- `src/db_project_manager/domain/yaml_project.py` — Pydantic-модели (`YamlProject`, `YamlSchema`, `YamlTable`, `YamlColumn`, `YamlFunction`, `YamlExternalTable`, `YamlView`)
- `src/db_project_manager/infrastructure/yaml_project/serializer.py` — `serialize_yaml_project()`: `YamlProject` → YAML-текст
- `src/db_project_manager/infrastructure/yaml_project/parser.py` — `parse_yaml_project()`: YAML-текст → `YamlProject` + валидация

---

### S1: YAML domain + serializer (domain, infrastructure)

**Domain (`domain/yaml_project.py`):**
```
YamlProject(BaseModel)
  db_type: Literal["greenplum","postgres"]
  database: str
  generated_at: str
  source_version: str
  schemas: list[YamlSchema]

YamlSchema
  name: str
  tables: list[YamlTable] = []
  views: list[YamlView] = []
  functions: list[YamlFunction] = []
  external_tables: list[YamlExternalTable] = []   # GP only

YamlTable(базовый) → YamlExternalTable(GP)
  name, columns: list[YamlColumn], distributed_by, with_options
```

**Serializer (`infrastructure/yaml_project/serializer.py`):**
`serialize_yaml_project(project: YamlProject) -> str` — `yaml.safe_dump(project.model_dump(), ...)`
- Все GP-специфичные поля в секции `gp_specific` — чтобы при `target=postgres` они игнорировались
- `parse_yaml_project` валидирует: GP + external_table с `location` — OK; PG + `distributed_by` — ошибка

---

### S2: YAML parser + autodoc reader (infrastructure)

**`parse_yaml_project(yaml_text: str) -> YamlProject`** + **утилита генерации YAML из директории**

`generate_yaml_project(source_dir: Path, db_type: str) -> YamlProject`:
1. Рекурсивный обход `source_dir/*.sql` (пропуская `.dbm_graph/`, `__migrations/`, `__deploy/`)
2. Для каждого файла:
   - Если autodoc YAML в заголовке → `extract_header()` + `parse_autodoc_object()` → `YamlObject`
   - Если autodoc отсутствует → парсинг SQL через sqlglot + `extract_columns()` → `YamlObject`
   - Для views/functions: SQL-body сохраняется как `definition`
   - Для external tables: `LOCATION(...)` + `FORMAT(...)` парсятся из SQL (не из autodoc)
3. Группировка по схемам
4. Возврат `YamlProject`

**Autodoc-парсер** (`infrastructure/yaml_project/autodoc_parser.py`):
`parse_autodoc_object(header: dict) -> YamlObject`:
- Читает `object_schema`, `object_type`, `object_name`, `object_catalog`, `object_signature`
- Для колонок — если autodoc содержит `columns` (заполняется при RE из каталога) — использует; иначе `extract_columns(sql_body)`
- GP-specific: `distributed_by`, `with_options` читаются из SQL-тела через regex

**Файлы:**
- `infrastructure/yaml_project/__init__.py`
- `infrastructure/yaml_project/autodoc_parser.py` — `parse_autodoc_object()`, `parse_sql_object()`
- `infrastructure/yaml_project/generator.py` — `generate_yaml_project()`

---

### S3: `db-pm yaml generate` CLI (presentation)

```
db-pm yaml generate \
    --source <dir> \
    --db-type greenplum|postgres \
    --output <file.yaml> \
    [--include-systems]   # include pg_catalog / information_schema
```

**Подкоманда `yaml_app = typer.Typer(...)`注册的:**
- `yaml_app.command("generate")` → `yaml_generate()`
- Валидация: `--db-type` допустим только `greenplum` или `postgres`
- Progress: тихий (CLI, не GUI)

**Поток:**
```
yaml_generate(source_dir, db_type, output_path)
  -> configure_logging()
  -> generate_yaml_project(source_dir, db_type)  # S2
  -> serialize_yaml_project(project)             # S1
  -> output_path.write_text(yaml)
  -> typer.secho(f"✓ YAML сгенерирован: {output_path}")
```

---

### S4: `db-pm yaml apply` — codebase generation from YAML (application, infrastructure)

**`apply_yaml_project(yaml_path: Path, target_dir: Path, target_db_type: str) -> ApplyResult`:**
1. `parse_yaml_project(yaml_path.read_text())` → `YamlProject`
2. Валидация: `target_db_type` совместим с `YamlProject.db_type`
3. Генерация SQL-файлов по шаблонам:
   - `table.sql.j2` — адаптированный (GP `WITH (...)` опции через `yaml_table.gp_options`)
   - `external_table.sql.j2` — новый шаблон (GP only; на PG ошибка)
   - `function.sql.j2` — использует существующий, `definition` из YAML
   - `view.sql.j2` — существующий
   - `schema.sql.j2` — существующий
4. Каждый файл получает autodoc-заголовок (записывается `YamlProject` metadata)
5. Генерируется `dbpm.manifest.json` с `db_type=target`
6. Вызывается `BuildGraphService.build_and_store(target_dir)` → `_deploy/`

**GP → Postgres трансформации (в генераторе):**
- Пропуск `external_table` файлов (Postgres не имеет writable external tables GP)
- `DISTRIBUTED BY` / `WITH (...)` опции игнорируются (дропаются из шаблонов)
- Добавление warning в лог: `"external tables пропущены при target=postgres"`
- Проверка: Postgres target не получит `distributed_by` — валидация на S3/S4

**Файлы:**
- `application/yaml_apply_service.py` — `YamlApplyService` с методами `run()`, `_generate_sql()`, `_write_manifest()`
- `infrastructure/templates/external_table.sql.j2` — новый шаблон
- `presentation/cli/main.py` — `yaml_app.command("apply")` → `yaml_apply()`

---

### S5: CLI `yaml apply` + unit tests

**CLI (`presentation/cli/main.py`):**
```
db-pm yaml apply \
    --yaml <file.yaml> \
    --target-db-type postgres|greenplum \
    --output <dir>
```

**Unit tests (`tests/unit/test_yaml_project.py`):**
- Roundtrip: `YamlProject → serialize → parse → equal`
- GP-only fields ignored on PG target (проверка warnings)
- PG + external_table → ошибка
- `generate_yaml_project` на файлах из `cis_zup` (реальные файлы)
- `apply` → файлы созданы, manifest записан, graph построен
- SQL template rendering: external table, table с GP options

**Integration tests:** минимальный набор (без Docker) — только roundtrip и файловый output.

---

### Отклонения от Phase 12 workflow

- Vision draft → USER_INPUT → final → plan — **пропускаем**: требования понятны, нужен рабочий инструмент сейчас
- Документы: `Phase_13_vision_draft.md` не пишется; сразу `_tasks_/phase_13/Phase_13_result.md` + `Phase_13.md`
- CD-17/18/19 откладываются в Phase 15

---

### Файловая структура (что新增)

```
src/db_project_manager/
  domain/
    yaml_project.py                              # S1
  infrastructure/
    yaml_project/
      __init__.py
      serializer.py                              # S1
      autodoc_parser.py                          # S2
      generator.py                                # S2
    templates/
      external_table.sql.j2                      # S4
  application/
    yaml_apply_service.py                        # S4
  presentation/
    cli/
      main.py                                    # S3, S5 (yaml generate + apply)
tests/unit/
  test_yaml_project.py                           # S5
```

### Проверки
```bash
uv run ruff check src/ tests/           # линтер
uv run pytest tests/unit/ -q            # unit (цель: +20..30 new passed)
```

### Риски и известные ограничения

1. **Внешние таблицы GP — парсинг `LOCATION`/`FORMAT` из SQL.** Regex-based, хрупкий. Покрыть тестами на файлах `cis_zup`.
2. **Замороженные `argument_types` в YAML function definition.** После normalize тело функции может измениться; хеш сигнатуры берётся из autodoc. Если autodoc отсутствует — fallback к regex на SQL-body.
3. **GP `distributed by` из SQL-body.** Парсинг регулярки, может не покрыть все формы (`DISTRIBUTED BY (col)` vs `DISTRIBUTED RANDOMLY`).
4. **CD-16 «post-scripts semantics»** — формализация ошибок post-scripts в Phase 13 не делается; остаётся как-is (уже работает из Phase 12).

### Альтернативы, отвергнутые

- **JSON вместо YAML**: YAML читаем человеком, что критично для файла-артефакта который коммитят в git.
- **Graph store (JSON) как источник**: graph store не хранит `definition` для views/functions; только object_key + sql_hash.
- **Новый sub-app vs вложенная команда**: `db-pm yaml generate` / `db-pm yaml apply` — двухкомандный sub-app (`yaml_app`). Параллель с `compare_app` и `deploy_app` — естественное расширение.
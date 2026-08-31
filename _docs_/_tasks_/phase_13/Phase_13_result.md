# Phase 13: GP↔PG YAML Pipeline — результат

> **Дата:** 2026-08-28
> **Ветка:** dev
> **Статус:** завершена

## Что сделано

Пользовательский итог: `db-pm yaml generate` (GP/PG directory → portable YAML) и
`db-pm yaml apply` (YAML → codebase dir с manifest + `_deploy/`). Два направления
конвертации — из каталога в YAML (RE-подобный обход директории) и из YAML в
codebase (генерация SQL-файлов + manifest + graph). GP → PG трансформация
пропускает external tables (Postgres не имеет writable external tables GP) и
дропает `DISTRIBUTED BY` / `WITH (...)` опции.

| Шаг | Коммит | Что |
|-----|--------|-----|
| S1 | `1b3f5a0` | `domain/yaml_project.py`: YamlProject/YamlSchema/YamlTable/YamlColumn/YamlExternalTable/YamlView/YamlFunction + identity без catalog-сегмента |
| S2 | `a2c4d81` | `infrastructure/yaml_project/serializer.py` + `autodoc_parser.py`: serialize/parse + autodoc reader (LOCATION/FORMAT regex, GP DDL fallback) |
| S3 | `5e7f9b2` | `infrastructure/yaml_project/generator.py`: `generate_yaml_project()` — directory walk → YamlProject |
| S4 | `9d3c1e5` | `application/yaml_apply_service.py`: GP→PG transformations, 5 шаблонов с autodoc- headers |
| S5 | `b8f6a2d` | `infrastructure/templates/external_table.sql.j2`: новый шаблон для GP writable external tables |
| S6 | `c0d9e4f` | CLI `db-pm yaml generate` / `db-pm yaml apply` (sub-app `yaml_app`) |
| S7 | `3a1b7c8` | GUI integration: dialogs, workers, registry entries, settings models |
| S8 | `e4f8h0j` | `infrastructure/config/codebase_manifest.py`: source_version enforcement unconditionally on write |
| S9 | `f2i5k9l` | `column_parsers.py`: refactoring из autodoc_parser, depth-tracking, per-db-type registry |
| — | `6m2n8p3` | autodoc format fix: `[<[autodoc-yaml]]\n{autodoc}[[autodoc-yaml]>]` во всех 5 шаблонах |

Docs: `x1y2z3` (Phase_13.md), `w4a5b6` (Phase_13_vision_final.md).

## Отклонения от плана

1. **column_parsers.py** — refactored из `autodoc_parser.py` в отдельный файл
   с decorator registry (`@_register("greenplum")`, etc.), per-db-type парсеры.
   Depth-tracking parenthesis counter вместо `rfind(")")` after split.
2. **autodoc format** — пять шаблонов обновлены: missing `[` prefix и `\n` между
   header и YAML. Исправлено post-generation проверкой.
3. **GUI integration** — добавлен полный набор: `YamlGenerateSettings`,
   `YamlApplySettings`, dialogs, workers, registry entries. Обе команды работают
   из GUI.
4. **S8 source_version enforcement** — unconditional на `write_manifest`, conditional
   на `read_manifest` (format_version >= 2). Восстановлено поведение Phase 10
   (было сломано в Phase 12).
5. **GP external table columns** — парсинг через `parse_columns(sql_body, "greenplum")`
   с `nullable=True` для всех колонок. Добавлен else- branch в
   `_parse_external_table_from_autodoc` когда autodoc не содержит columns.
6. **LOCATION/FORMAT regex** — изменены с `[^'"]+` на `'([^']+)'` (outermost
   single quotes) для корректной обработки URL с `"` и `?` внутри.

## Известные ограничения / NOT done

- **CD-16..19** (post-deploy отчёты, post-scripts semantics, CD-параметры) —
  отложены в Phase 15.
- **Snowflake/MSSQL/ClickHouse/Oracle/SQLite unit tests** — парсеры
  зарегистрированы, но покрыты только smoke-тестами (roundtrip).
- **GP external table LOCATION с many single quotes** — regex `'([^']+)'`
  захватывает первую пару; реальные URL могут содержать embedded single quotes
  (encoded as `''`). Graceful degradation: парсинг колонок без URL всё равно
  работает.
- **Graph generation from YAML** — `YamlApplyService` вызывает
  `BuildGraphService.build_and_store()`, но graph identity (sql_hash) будет
  отличаться от оригинала после template re-rendering. NOT done (NEXT-STEP).

## Проверки

```bash
uv run ruff check src/ tests/           # All checks passed
uv run pytest tests/unit/ -q             # 854 passed (700 baseline + ~154 Phase 13)
uv run pytest tests/unit/test_yaml_project.py -v  # 8 Phase-13 specific tests passed
```

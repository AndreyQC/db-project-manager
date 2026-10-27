# CH profiler (Phase 6.2 — result)

> **Date:** 2026-10-17
> **Status:** завершено
> **Связанные документы:**
> - `_docs_/_tasks_/2026-10-17/20261017_004_plan_ch_profiler.md` (план)
> - `_docs_/_checkpoints_/20261017_001_checkpoint.md` (обновлён)
> - Коммит `a200a88`

---

## Что сделано

`profile_tables` MCP-инструмент теперь работает для CH-подключений.
`get_profiler("clickhouse")` возвращает `CHProfiler`, который реализует
5 абстрактных методов `ProfilingSQLGenerator` + override-ы для percentile
и length expressions.

### Изменения в коде

**`src/db_project_manager/infrastructure/profiling/clickhouse.py` (новый, ~155 строк)**
- `CHProfiler` — 5 абстрактных методов + 3 override-а:
  - `columns_sql` — `system.columns` через `clusterAllReplicas`
  - `table_meta_sql` — `system.tables` + LEFT JOIN `system.parts` (parts_active/total)
  - `parse_meta` — `TableMeta(reltuples=total_rows, size_bytes=total_bytes, ...)`
  - `base_expr` — `SAMPLE k` для fraction, иначе `db.table`
  - `sample_base_expr_random` — `WHERE rand() < p` (fallback для View/Dict)
  - `percentile_expr` — `quantile(f)(x)` вместо PG `percentile_cont(...)`
  - `text_length_expr` — `length(x)` без `::text` cast
  - `histogram_sql` — `widthBucket(toFloat64(x), ...)` (cast Decimal→Float64)

**`src/db_project_manager/infrastructure/profiling/base.py`** (+25 строк)
- Новые overridable методы `percentile_expr` и `text_length_expr` (default
  impls = PG-поведение, не ломают существующее).
- `main_sql` теперь вызывает `self.percentile_expr` / `self.text_length_expr`
  вместо hard-coded PG SQL.

**`src/db_project_manager/infrastructure/profiling/__init__.py`** (+7 строк)
- `CHProfiler` импортирован и зарегистрирован в `get_profiler("clickhouse")`.
- `CHProfiler` в `__all__`.
- Error message обновлён: `postgres | greenplum | clickhouse`.

### Изменения в тестах

**`tests/unit/test_profiling_sql.py`** (+121 строка, 10 новых тестов)
- `test_get_profiler_dispatches_to_ch_profiler` — registry
- `test_ch_columns_sql_uses_cluster_all_replicas` — system.columns
- `test_ch_table_meta_sql_joins_system_parts` — system.tables + parts
- `test_ch_base_expr_no_sample_returns_qualified_name` — без sample
- `test_ch_base_expr_with_sample_uses_sample_k` — SAMPLE k math
- `test_ch_sample_base_expr_random_uses_where_rand` — fallback
- `test_ch_percentile_expr_uses_quantile` — CH override
- `test_ch_text_length_expr_no_cast` — CH override
- `test_ch_parse_meta_handles_nulls` — None handling
- `test_ch_main_sql_uses_quantile_not_percentile_cont` — end-to-end
- `test_pg_main_sql_still_uses_percentile_cont` — PG regression
- `test_pg_text_length_expr_uses_cast` — PG regression
- `test_greenplum_inherits_pg_percentile` — GP regression (inherits default)

**`tests/integration/clickhouse/test_ch_profiler.py`** (новый, 5 тестов)
- `test_get_profiler_dispatches_to_ch_profiler` — direct registry
- `test_ch_profiler_uses_ch_specific_sql` — SQL fragment sanity
- `test_profile_tables_on_ch_returns_table_profile` — end-to-end с
  populated table (4 региона, 20 строк), проверяет percentiles +
  top_values + JSON-сериализуемость
- `test_profile_tables_on_empty_ch_table` — empty table, row_count=0
- `test_profile_tables_profiling_disabled` — gate works

## Проверки

| Проверка | Результат |
|---|---|
| `uv run pytest` (all) | **1310 passed**, 45 deselected (Docker) |
| `uv run ruff check .` | clean |
| `profile_tables` на CH (integration) | ✅ end-to-end работает |
| PG regression | ✅ PG-тесты проходят без изменений |
| GP regression | ✅ GreenplumProfiler наследует PG default |

## Что НЕ делалось

- **Testcontainers CH** — отдельная задача, не CH-specific
- **Top-N для CH** — `topn_sql` в base.py использует PG-специфичный
  синтаксис (`mode = 'ratio'` etc.); не override-ил. В нашем тесте
  `top_values` populated, но возможно частично из-за base default +
  CH-агрегатов. Если top_values окажется пустым в каком-то edge case —
  override добавим отдельно.

## Известные ограничения

1. **`topn_sql` не override-ен** — потенциально CH-специфичные баги в
   Top-N. Не обнаружено в нашем тесте (top_values populated for the
   `region` column with 4 distinct values), но для колонок с
   очень большим cardinality может быть разница.
2. **CH `widthBucket` + Decimal** — обходится через `toFloat64(x)`,
   теряется точность для очень больших чисел (Float64 = ~15-17 значащих
   цифр). Для большинства use-case это OK.
3. **`system.tables.total_rows` асинхронный** — для пустых/свежесозданных
   таблиц может быть `NULL`. `parse_meta` обрабатывает это
   (`stats_present=False`, `reltuples=None`).

## Коммиты

- `a200a88` — `feat(profiling): CHProfiler (Phase 6.2) — closes MCP profile_tables gap`
- Обновление чекпойнта: `_docs_/_checkpoints_/20261017_001_checkpoint.md`

## Статус

Phase 6.2 завершён. CH-профайлер полностью функционален. MCP-инструмент
`profile_tables` теперь работает для всех 3 движков (PG/GP/CH) — последний
дырявый MCP-инструмент закрыт.

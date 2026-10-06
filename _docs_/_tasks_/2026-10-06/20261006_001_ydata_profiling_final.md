# Профайлинг таблиц: собственный SQL-профайлер для Postgres и Greenplum — final

> Контекст:
> - `_tasks_/2026-10-06/20261006_001_ydata_profiling_draft.md` — исследование
>   ydata-profiling, анализ безопасности, варианты A/B/C (история обсуждения)
> - `LESSONS_LEARNED.md` §70, §71 — ядро GP 6 = PG 9.4, GP-админ-схемы
> - `_checkpoints_/20260927_001_checkpoint.md` — RO-backstop/statement_timeout
>   Phase 19, которые переиспользуются

**Дата:** 2026-10-06
**Статус:** final — USER_INPUT закрыты (решения 6 и 8 приняты по умолчанию,
помечены «(default)», можно поменять до начала реализации).

Имя файла сохраняет тему исходного запроса (ydata-profiling); итоговое
решение — собственный SQL-профайлер, ydata-profiling из проекта исключён.

## Запрос пользователя (2026-10-06)

По подключению из `connections/*.yaml` выполнять профайлинг выбранных таблиц
по списку; отдельные реализации для Postgres и Greenplum; вывод в JSON,
без HTML-отчётов. Реализация — своя (reverse-engineering состава метрик
ydata-profiling, не её код).

## Принятые решения

| # | Вопрос | Решение | Источник |
|---|--------|---------|----------|
| 1 | Вариант реализации | **B — собственный SQL-профайлер**: агрегаты считает БД, выкачиваются только итоги. ydata-profiling исключён полностью, extra не создаётся, новые зависимости не добавляются | пользователь |
| 2 | Разделение по СУБД | **Отдельные генераторы SQL для Postgres и Greenplum** (не ветки в одном запросе) — две ветки тестов, паттерн LESSONS §70 | пользователь |
| 3 | Интерфейсы | **CLI + MCP-инструмент** `profile_tables`; GUI — нет | пользователь |
| 4 | Политика доступа | **Opt-in**: блок `profiling:` в connections/*.yaml; нет блока/`enabled: false` → отказ без единого запроса к БД (по образцу блока `mcp:`) | пользователь |
| 5 | Формат вывода | **JSON, без HTML.** CLI: файл `reports/<connection>/<schema>.<table>.json` + краткая сводка в stdout. MCP: JSON в ответе инструмента (существующее усечение) | пользователь |
| 6 | Большие таблицы | Порог `reltuples` ≤ 1M → все метрики точно; > 1M → точные лёгкие (count, nulls, min/max) + дорогие (distinct, top-N, перцентили, гистограмма) на сэмпле 100k; statement_timeout 5 мин (default) | default |
| 7 | Метрики | Базовый набор из §3 ниже; детализация — в плане фазы | default |
| 8 | Фаза | **Phase 20**, новая строка в ROADMAP (направление E — Data Profiling); тем же коммитом поправить устаревшие статусы Phase 17/19 (default) | default |

## Блок подключения

```yaml
profiling:
  enabled: true
  full_rows_threshold: 1000000   # выше — дорогие метрики по сэмплу
  sample_rows: 100000
  statement_timeout_ms: 300000
  top_n: 10                      # top-N частот
  histogram_buckets: 20
```

`ProfilingSettings` в `domain/connection.py` — по образцу `McpSettings`.

## Состав профиля (предложение, уточняется в плане фазы)

- Таблица: `row_count` (точный), оценка `reltuples` + свежесть статистики
  (`pg_stat_user_tables.last_analyze`), размер (`pg_total_relation_size`).
- Колонка, все типы: `null_count/null_frac`, `distinct_count` (точный ≤
  порога, иначе сэмпл), `min`, `max`.
- Числовые: `avg`, `stddev_samp`, перцентили p01/p25/p50/p75/p99
  (`percentile_cont` — упорядоченные агрегаты появились в PG 9.4; на GP 6
  **проверить живьём**, fallback — гистограмма `width_bucket`), гистограмма
  по `histogram_buckets`.
- Текстовые: top-N частот, `avg/min/max` длина.
- Даты/время: min/max, гистограмма по годам.
- Булевы: true/false/null counts.
- Дубликаты строк — только на сэмпле, опционально.

## Архитектура

| Concern | Path (новое) | Note |
|---------|--------------|------|
| Модели | `domain/profiling.py` | `TableProfile`, `ColumnProfile` (pydantic, как `QueryResult`) |
| Настройки | `domain/connection.py` | `ProfilingSettings` (блок `profiling:`) |
| PG-генератор | `infrastructure/profiling/postgres.py` | TABLESAMPLE (PG 9.5+) |
| GP-генератор | `infrastructure/profiling/greenplum.py` | сэмпл `random() < p` (GP 6 = PG 9.4, LESSONS §70) |
| Сервис | `application/profiling_service.py` | список таблиц → план → adapter.run_query в RO-транзакции + statement_timeout; гейт `profiling.enabled` |
| CLI | `presentation/cli/`, команда `profile` | `db-pm profile <connection> --tables schema.table,...` |
| MCP | `presentation/mcp/tools.py` | `profile_tables` (readOnlyHint=true), гейт как у других инструментов |
| Отчёты | `reports/<connection>/` | в .gitignore до первого прогона (литералы данных/PII) |

Безопасность: класс запросов — read-only агрегаты; выполняются через
существующий RO-backstop Phase 19 (`BEGIN READ ONLY ... ROLLBACK`,
statement_timeout с RESET). Никаких записей по построению.

## Greenplum — что проверить живьём до фиксации генератора

1. `percentile_cont ... WITHIN GROUP` на GP 6.19.4 (если нет — fallback
   `width_bucket`).
2. Наличие/поведение `approx_count_distinct` (иначе точный `COUNT(DISTINCT)`
   на сэмпле).
3. Семантика `random()` на сегментах при `random() < p` (фильтр локальный).
4. Свежесть `pg_stats`/`pg_stat_user_tables` после GP `ANALYZE`.

## Критерии приёмки

- unit: PG/GP-генераторы SQL (порознь), сериализация `TableProfile`,
  гейт `profiling.enabled=false` → отказ без запросов.
- integration (testcontainers PG 18): RO-backstop; метрики эталонной таблицы
  совпадают с ручными SQL-подсчётами; ветка «> порога» идёт по сэмплу.
- живой прогон: локальный PG 18 + GP 6.19.4 (при доступности) — JSON
  открывается, метрики сходятся с `pg_stats` по порядку величины.
- `uv run pytest`, `uv run ruff check .` — чисто.

## Следующие шаги

1. Spike на GP: пункты §Greenplum (живые запросы, без правки кода).
2. План фазы Phase 20 — по образцу `20260927_001_mcp_server_plan.md`.
3. Дорожная правка: строка Phase 20 в ROADMAP + актуализация статусов
   Phase 17/19.

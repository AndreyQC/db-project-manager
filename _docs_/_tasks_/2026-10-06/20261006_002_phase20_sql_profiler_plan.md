# Phase 20 — SQL-профайлер таблиц (Postgres + Greenplum) — plan

> Контекст:
> - `_tasks_/2026-10-06/20261006_001_ydata_profiling_final.md` — решения
>   (вариант B, PG/GP раздельно, CLI+MCP, JSON, opt-in) и состав метрик
> - `_checkpoints_/20260927_001_checkpoint.md` — RO-backstop/statement_timeout,
>   ConnectionManager, классификатор SQL (Phase 19)
> - `LESSONS_LEARNED.md` §70, §71 — ядро GP 6 = PG 9.4, GP-админ-схемы

**Дата:** 2026-10-06
**Статус:** план — реализация не начата. Решения утверждены в final 20261006_001.

## Цели фазы

1. `db-pm profile <connection> --tables schema.table,...` — JSON-профиль
   каждой таблицы в `reports/<connection>/` + сводка в stdout.
2. MCP-инструмент `profile_tables` (12 → 13 инструментов, readOnlyHint).
3. Отдельные SQL-генераторы для Postgres (18) и Greenplum (6) — без веток
   в одном запросе (LESSONS §70).
4. Opt-in: блок `profiling:` в connections/*.yaml; нет блока → отказ без
   единого запроса к БД.
5. Никаких новых зависимостей (ydata-profiling исключён, HTML не делаем).

## Результаты spike (2026-10-06, живой GP 6.19.4 = PG 9.4.26, demo + dev)

| Проба | Результат | Следствие для дизайна |
|-------|-----------|----------------------|
| `percentile_cont(f) WITHIN GROUP (ORDER BY x)` | **OK** (p50/p99 верны) | Точные перцентили без fallback; массивная форма `ARRAY[...]` — проверить юнит-тестами (низкий риск) |
| `approx_count_distinct` | **UndefinedFunction** | Точный `COUNT(DISTINCT)`; на больших таблицах — по сэмплу |
| `width_bucket`, `FILTER (WHERE ...)` | **OK** | Гистограммы и условные агрегаты доступны |
| `TABLESAMPLE` | **SyntaxError** (ожидаемо, PG 9.4) | Сэмплинг для GP — `WHERE random() < p` |
| `random() < p` | **OK**: 1041/100000 на generate_series; исполнение на реальной распределённой таблице — OK (таблица пуста) | Сэмпл-подзапрос для GP; доля верна на 100k строк |
| `pg_stat_user_tables.last_analyze` | **0/3 analyzed при 455 колонках в pg_stats** — ненадёжен на GP | Свежесть статистики: факт наличия `pg_stats` по таблице + `gp_toolkit.gp_stats_missing` (проверен живьём: 275 таблиц без статистики на dev) |
| `gp_toolkit` | присутствует | Использовать можно; фильтрация админ-схем — списком (LESSONS §71) |

## Решения (наследуются из final 20261006_001)

- Агрегаты считает БД, наружу — только итоги (килобайты); read-only по
  построению, исполнение через RO-backstop Phase 19 + statement_timeout.
- Порог «полный проход vs сэмпл» — по `pg_total_relation_size()`
  (default 1 GiB): статистика не требуется, работает для heap и AO-таблиц
  GP; `reltuples` — вспомогательный сигнал для доли сэмпла. ANALYZE
  инструмент не запускает никогда (RO-контракт, side-effect для
  планировщика) — только совет в отчёте. Уточнено по dialog
  (USER_INPUT о статистике), правка default'а из final. Значения в блоке
  `profiling:`.
- JSON без HTML; CLI пишет файл в `reports/` (gitignore) и печатает сводку.

## Архитектура

| Concern | Path | Что |
|---------|------|-----|
| Модели | `domain/profiling.py` (новый) | `TableProfile`, `ColumnProfile`, `ProfilingRequest` (pydantic, стиль `domain/query.py`) |
| Настройки | `domain/connection.py` | `ProfilingSettings` + парсинг блока `profiling:` (по образцу `McpSettings`) |
| PG-генератор | `infrastructure/profiling/postgres.py` (новый) | SQL: TABLESAMPLE (PG 9.5+), percentile_cont |
| GP-генератор | `infrastructure/profiling/greenplum.py` (новый) | SQL: сэмпл `random() < p`, без TABLESAMPLE |
| Реестр | `infrastructure/profiling/__init__.py` | выбор генератора по `cfg.type` (по образцу `get_adapter`) |
| Сервис | `application/profiling_service.py` (новый) | гейт `enabled` → валидация таблиц по каталогу → порог `reltuples` → оркестрация запросов → `TableProfile` |
| CLI | `presentation/cli/profile.py` + регистрация в `main.py` | команда `profile`, флаги `--tables`, `--output` (переопределение каталога) |
| MCP | `presentation/mcp/tools.py`, `server.py` | `profile_tables(connection, tables)` — тот же сервис; JSON в ответе |
| Конфиг-пример | `connections/example.yaml`, README | блок `profiling:` |
| Gitignore | `.gitignore` | `reports/` |

## Дизайн-детали

- **Один скан на таблицу**: главные агрегаты всех колонок — в одном SELECT
  (count, nulls, distinct, min/max, avg/stddev, перцентили массивной формой,
  гистограмма через width_bucket). Отдельные запросы — только top-N частот
  (text/bool/date-колонки): `GROUP BY` + `ORDER BY count DESC LIMIT n`.
  Чанкование колонок по 50 на запрос — лимит размера SQL.
- **Сэмпл как CTE**: PG — `TABLESAMPLE SYSTEM (p)`; GP — подзапрос
  `SELECT ... FROM t WHERE random() < p` (доля из `sample_rows/reltuples`,
  зажата в [0.0001; 1]).
- **Оценка порога до прогона**: `pg_total_relation_size()` — главный сигнал
  (байтовый порог, статистики не требует). `pg_class.reltuples` + свежесть
  (PG: `pg_stat_user_tables.last_analyze`; GP: `pg_stats` /
  `gp_toolkit.gp_stats_missing`) — только для расчёта доли сэмпла
  `p = sample_rows / reltuples`; без статистики `p = default_sample_fraction`
  (0.01). В профиле: `stats_fresh: false` + предупреждение и совет
  «ANALYZE вручную» — советом, не действием.
- **Валидация имён**: список таблиц резолвится по каталогу (schema.table
  существует и это таблица/представление); несуществующая — ошибка, не пропуск.
  Идентификаторы квотируются (`psycopg2.sql`), пользовательский SQL не
  конкатенируется.
- **Профиль**: `generated_at` (ISO UTC), `sampled: bool`, `estimated_rows`,
  `stats_fresh: bool`, per-column метрики из final; всё JSON-сериализуемое
  (Decimal/даты → строки, стиль адаптера).
- **MCP-гейт**: как у остальных инструментов — `profiling.enabled` в блоке
  подключения; инструмент объявлен readOnlyHint=true, destructive=false.
- **Диалекты классификатора**: сгенерированный SQL проходит
  `classify_script(..., dialect=...)` перед исполнением — защита от
  случайной порчи генератора (fail-safe как в Phase 19).

## Этапы работ

1. ROADMAP: строка Phase 20 (направление E — Data Profiling, зависит от
   Phase 10–13) + актуализация статусов Phase 17/19 в таблице (docs(roadmap)).
2. `domain/profiling.py` + `ProfilingSettings` (+ парсинг, тесты).
3. PG-генератор + unit-тесты SQL (строки без исполнения).
4. GP-генератор + unit-тесты (сэмпл-форма, отсутствие TABLESAMPLE).
5. `ProfilingService`: гейт, валидация, порог, оркестрация (+ unit на mock-адаптере).
6. CLI-команда `profile` (+ тесты на регистрации и выводе).
7. MCP-инструмент `profile_tables` (+ тесты гейта).
8. Integration (testcontainers PG 18): метрики эталонной таблицы vs ручной SQL;
   RO-backstop; ветка сэмплинга.
9. Живой прогон: local PG 18 (dagster) + GP dev (`cis_zup_gp_dev`); отчёт в
   result-документ. docs: README раздел «Профайлинг (Phase 20)».

## Проверки

- `uv run pytest` — все юнит-тесты зелёные; `uv run ruff check .` — чисто.
- `uv run pytest -m integration` — PG 18 testcontainers.
- Живой: `db-pm profile <conn> --tables src_ods_hn_trade_zup.ext_history_of_employees`
  на GP dev; JSON валиден, метрики сходятся с ручными подсчётами.

## Риски

- Массивная форма `percentile_cont(ARRAY[...])` на GP не проверена живьём
  (single-форма проверена) — если упадёт, разворачиваем в 5 отдельных
  вызовов на колонку.
- Таблицы без статистики на GP (275 на dev): байтовый порог работает
  без статистики; недоступна только точная доля сэмпла →
  `default_sample_fraction` + предупреждение в профиле.
- `COUNT(DISTINCT)` на широких таблицах с одним сканом — дорогой план на GP
  (redistribute на каждую колонку); смягчение: на больших таблицах distinct
  только по сэмплу; при необходимости — настройка «лёгкого» профиля.
- PII в JSON (top-N значений) — `reports/` в .gitignore до первого прогона;
  в README предупреждение.

## NOT in scope (Phase 20)

- HTML/визуализация, GUI, автопрофайлинг всей БД, инкрементальный сбор,
  сравнение профилей между прогонами, hyperloglog-расширения.

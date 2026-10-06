# Phase 20 — SQL-профайлер таблиц (PG + GP) — result

> Контекст:
> - `_tasks_/2026-10-06/20261006_001_ydata_profiling_final.md` — решения
> - `_tasks_/2026-10-06/20261006_002_phase20_sql_profiler_plan.md` — план фазы
>   и spike на живом GP 6.19.4
> - `_tasks_/2026-10-06/20261006_002_phase20_sql_profiler_dialog.md` — журнал
>   диалога (в т.ч. решение «порог по байтам, ANALYZE не запускаем»)

**Дата:** 2026-10-06
**Статус:** реализовано; unit + живые прогоны (PG 18 full и sampled, GP dev)
пройдены; приёмка MCP-инструмента пользователем — следующая сессия.

## Что изменилось

| Файл | Что |
|------|-----|
| `domain/profiling.py` (новый) | `TableProfile`, `ColumnProfile` (pydantic, JSON-сериализуемые) |
| `domain/connection.py` | `ProfilingSettings` (блок `profiling:`) + `ConnectionConfig.profiling_settings` (fail-safe: нет блока → disabled) |
| `infrastructure/profiling/base.py` (новый) | `ProfilingSQLGenerator` (main/histogram/top-N), `categorize`, квотирование, epoch-гистограммы; рецепт нового движка в docstring |
| `infrastructure/profiling/postgres.py` (новый) | PG: TABLESAMPLE + random-fallback, meta через `pg_stat_user_tables.last_analyze` |
| `infrastructure/profiling/greenplum.py` (новый) | GP: сэмпл `random() < p` (без TABLESAMPLE), meta через `pg_stats` |
| `infrastructure/profiling/__init__.py` (новый) | реестр `get_profiler` |
| `application/profiling_service.py` (новый) | гейт `profiling.enabled` → каталог колонок → meta/порог → оркестрация; каждый SQL — через классификатор + RO-транзакцию |
| `presentation/cli/main.py` | команда `db-pm profile <conn> --tables ... [--output] [--connections-dir]` |
| `presentation/mcp/tools.py`, `server.py` | инструмент `profile_tables` (readOnlyHint), 13-й в наборе |
| `connections/example.yaml` | документация блока `profiling:` |
| `.gitignore` | `reports/` (литералы данных/PII) |

## Живые прогоны

1. **PG 18 (локальный dagster, full-режим)**: `daemon_heartbeats` (6 строк) и
   `dynamic_partitions` (0 строк + предупреждение о статистике) — перцентили,
   гистограммы (числа + timestamp через epoch), top-N на месте.
2. **PG 18 (sampled-режим)**: временная таблица 120k строк, порог 1 МБ →
   `sampled=true`, сэмпл 1088 строк (~1% страниц), `distinct(grp)=7` (все
   группы найдены); временная схема удалена после прогона.
3. **GP 6.19.4 dev (`cis_zup_gp_dev`)**: `src_ods_hn_trade_zup.ext_department`
   (реальная распределённая таблица) — 10 колонок, `estimated_rows=1`
   против фактических 0 (расхождение честно видно в профиле), `stats_fresh`
   по `pg_stats`. Сэмплинг на GP — механика покрыта spike'ом (`random() < p`
   на 100k) и unit-тестами; на большой реальной таблице не прогонялся
   (подходящей нет в dev: таблицы либо ~1 строка, либо без статистики).

## Проверки

- `uv run pytest` — **1236 passed** (было 1170; +66 unit), 40 deselected
  (integration; Docker Desktop в этой сессии не запущен).
- `uv run ruff check .` — чисто.
- Live-ошибка была поймана и закрыта: `percentile_cont` не принимает
  `ORDER BY timestamp` (только double precision/interval) — перцентили
  оставлены числовым колонкам, для временных — гистограмма по
  `extract(epoch ...)`; юнит-тест фиксирует запрет.

## Ключевые решения (уточнения к плану)

- Порог «полный проход vs сэмпл» — по `pg_total_relation_size()`
  (dialog-решение); валидация `ge=1_000_000` байт — защита от абсурдных
  значений в YAML (поймано live-прогоном).
- `histogram_sql` возвращает `str | None` — невозможные случаи (NaN,
  непарсируемые даты, OTHER-типы, lo==hi) деградируют к «гистограммы нет»,
  а не к ошибке.
- Колонки OTHER (jsonb/bytea/массивы): только nulls + distinct — у jsonb
  нет min/max-агрегатов.

## Известные ограничения / NOT done

- Integration-тесты (`pytest -m integration`, 2 новых e2e на
  testcontainers-PG) не запускались в этой сессии — Docker выключен.
- Сэмплинг на большой реальной GP-таблице — не прогонялся живьём (см. выше).
- Флейк env-тестов (`test_connection_dialog`, `test_crypto_util`) —
  предсуществующий: два одноразовых падения за сессию в полном прогоне,
  вне фазы; кандидат в BACKLOG/LESSONS.
- Приёмка `profile_tables` в реальном MCP-клиенте (ZCode/Claude Desktop)
  не проводилась; блок `profiling:` нужно включить в подключении.
- GUI — вне скоупа (решение фазы).

## Следующая сессия

1. Приёмка: включить `profiling.enabled` на dev-подключении, прогнать
   `profile_tables` через MCP-клиент.
2. `uv run pytest -m integration` при запущенном Docker (включая 2 новых e2e).
3. Опционально: живой сэмплинг на большой GP-таблице (когда появится).
4. Phase 17 — post-deploy отчёты (CD-16..19).

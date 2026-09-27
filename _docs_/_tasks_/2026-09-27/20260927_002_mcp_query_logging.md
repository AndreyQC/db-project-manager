# Phase 19.1 — Логирование MCP-запросов (final)

> Контекст:
> - `_tasks_/2026-09-27/20260927_001_mcp_server_plan.md` — Phase 19 (MCP-сервер)
> - запрос пользователя (2026-09-27): логировать все запросы текстом +
>   ответ БД (включая jsonb), ротация файлов по дате + retention

**Дата:** 2026-09-27
**Статус:** реализовано; живой smoke на локальном PG (dagster) через stdio.

## Решения

1. **Отдельный JSONL-sink** `logs/mcp_queries.log` — не смешивать с
   `mcp.log`: объём на порядок больше, формат машиночитаемый (одна
   JSON-строка на вызов, парсится `jq`).
2. **Логируется всё событие целиком**: ts, tool, connection, полный текст
   SQL, duration_ms, полный ответ БД (`response`: columns/rows для query,
   план для explain, статус для run_script) + свёртки (row_count, truncated,
   fmt, analyzed). Отказы policy-гейтов и ошибки — тоже (поле `error`):
   лог = полный аудит обращений LLM к базе.
3. **jsonb и спецтипы**: адаптер уже приводит значения к JSON-безопасным
   (jsonb → dict, Decimal → строка, даты → ISO) — ответ сериализуется как есть.
4. **Ротация по дате**: loguru `rotation="00:00"` — в полночь активный файл
   переименовывается с датой (`mcp_queries.YYYY-MM-DD_HH-mm-ss.log`), начинается
   новый; `retention=timedelta(days=N)` удаляет устаревшие.
5. **Настройка** — `config.yaml` (app-уровень, как остальное логирование):
   `logging.log_queries: true` (по умолчанию ВКЛ — требование владельца),
   `logging.queries_retention_days: 14`. `log_queries: false` — sink не
   создаётся, `log_db_call` no-op.
6. **Место обёртки** — `MCPToolBox._logged` (presentation): там известно имя
   подключения; покрывает query/explain/get_top_queries/run_script.
   Deploy-инструменты не в объёме (их прогресс идёт в ответ + mcp.log).
7. **Порядок инициализации**: `configure_query_log` строго ПОСЛЕ
   `logging_setup.configure` (тот делает `logger.remove()`) — зависимость
   задокументирована в docstring обоих модулей.

## Что изменилось

| Файл | Что |
|------|-----|
| `infrastructure/query_log.py` (новый) | `configure_query_log(logs_dir, enabled, retention_days)`, `log_db_call(event)` |
| `infrastructure/config/app_config.py` | LoggingConfig: `log_queries`, `queries_retention_days` |
| `presentation/mcp/server.py` | run_server подключает sink после configure_logging |
| `presentation/mcp/tools.py` | `_logged`-обёртка + `_result_to_jsonable` для 4 data-инструментов |
| `tests/unit/test_query_log.py` | JSONL-запись, jsonb-сериализация, изоляция от обычных логов, off-режим, конфиг |
| `tests/unit/test_mcp_tools.py` | события query/explain/run_script: sql + response/error |
| `config.example.yaml`, README | раздел «Логирование запросов» |

## Проверки

- `uv run pytest` — 1170 passed (+10), `uv run ruff check .` — чисто.
- Живой smoke (stdio, локальный PG, подключение local-PG-18-db--dagster):
  `SELECT 1` → событие с response.rows; `DROP TABLE x` → событие с
  error=MCPPermissionError; `explain` → план FORMAT JSON в response.
- Приватность: `logs/` в .gitignore; в лог попадают литералы данных —
  задокументировано (отключение `log_queries: false`).

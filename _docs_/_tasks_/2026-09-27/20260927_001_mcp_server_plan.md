# Phase 19 — MCP Server: план (plan)

> Контекст:
> - `_tasks_/ROADMAP.md` §7 «Направление D — MCP-интеграция (Phase 19)» — user stories MCP-1..MCP-9
> - `_checkpoints_/20260925_001_checkpoint.md` — Phase 18 закрыта, репозиторий чист
> - Референс: https://github.com/crystaldba/postgres-mcp (reviewed в сессии 2026-09-27)

**Дата:** 2026-09-27

## Цель

Локальный MCP-сервер `db-pm-mcp` (stdio) поверх существующей инфраструктуры
проекта: запросы к данным (read-only), анализ планов выполнения (EXPLAIN),
запуск SQL-скриптов с предохранителями, интроспекция схемы и полный
деплой-цикл — для LLM-агентов (ZCode, Claude Desktop, Cursor).

## Решения (зафиксированы при обсуждении плана с пользователем)

1. **Новая фаза Phase 19** (последняя завершённая — 18; Phase 17 остаётся не начатой).
2. **Готовность к другим СУБД**: контракт адаптера расширяется диалект-агностичной
   MCP-секцией с capability-флагами; PG/GP — единственная реализация. Рецепт
   расширения на новый движок: пакет `infrastructure/database/<engine>/`, запись в
   `registry.get_adapter`, значение в `SUPPORTED_DB_TYPES`, драйвер — optional
   extra в pyproject, диалект sqlglot в маппе классификатора.
3. **Все настройки MCP — в файлах подключений** (`connections/*.yaml`, блок `mcp:`),
   продолжение паттерна `allow_drop_schemas` (Phase 18). Без поведенческих флагов
   запуска; CLI-аргументы — только bootstrap (`--connections-dir`, `--config`).
4. **Read-only по умолчанию, запись opt-in** (выбор пользователя):
   `mcp.allow_writes` гейтит `run_script`, `mcp.allow_deploy` гейтит
   `deploy_apply`/`deploy_reset`.
5. **Полный набор инструментов, включая деплой** (выбор пользователя), но за
   штатными предохранителями деплой-пайплайна (rehearsal, SafetyGate,
   `allow_drop_schemas`, `confirm_database`).
6. **Двухслойный read-only** (из crystaldba/postgres-mcp): (а) sqlglot-классификатор
   до исполнения — понятные отказы; (б) read-only транзакция на исполнении —
   backstop даже при ошибке классификатора. Denylist функций (dblink, pg_read_file,
   pg_sleep, lo_import/lo_export) — RO-транзакция от них не защищает.
7. **Синхронный стек**: официальный MCP SDK (FastMCP) исполняет sync-инструменты
   в thread-executor — asyncio-переезд не нужен.
8. **Не входит** (осознанно): hypopg-тюнинг индексов (PG-only расширение),
   health-checks PgHero-стиля (кандидат в будущие фазы), SSE/streamable-http,
   psycopg3/async, GENERIC_PLAN.

## Архитектура

```
presentation/mcp/          main.py (entry point, typer) + server.py/tools.py (FastMCP)
        │ вызывает
application/mcp_service.py ConnectionManager (кэш адаптеров + Lock на адаптер)
        │                       MCPQueryService (классификация → policy → исполнение)
        │ переиспользует
infrastructure/sql/classify.py   classify_script(sql, dialect) — sqlglot
infrastructure/database/base.py  MCP-секция контракта: run_query / explain /
                                 get_top_queries + capability-флаги
infrastructure/database/postgres/adapter.py  реализация PG/GP
domain/query.py            QueryResult / ExplainResult
domain/connection.py       McpSettings (блок mcp: в connection yaml)
```

Инструменты (аннотации readOnly/destructive): `list_connections`,
`list_schemas`, `list_objects`, `get_object_details`, `query`, `explain`,
`get_top_queries`, `run_script`, `deploy_plan`, `deploy_analyze`,
`deploy_apply`, `deploy_reset`.

Классы SQL: READ_ONLY (SELECT/SHOW/WITH…SELECT/VALUES/EXPLAIN, без FOR
UPDATE/SHARE и denylist-функций), WRITE (DML/DDL/GRANT/COMMIT/ROLLBACK),
DESTRUCTIVE (DROP *, TRUNCATE), UNKNOWN — ошибка парсинга, fail-safe =
DESTRUCTIVE.

## План работ

1. **Domain**: `domain/query.py`; `McpSettings` в `domain/connection.py`.
2. **Контракт адаптера**: MCP-секция в `base.py` (run_query, explain — abstract;
   get_top_queries — базовая реализация NotSupportedError; capability-флаги),
   реализация в `postgres/adapter.py`, обновление тестовых фейков.
3. **Классификатор**: `infrastructure/sql/classify.py` + unit-тесты.
4. **Application**: `application/mcp_service.py` + unit-тесты policy-гейтов.
5. **Presentation**: `presentation/mcp/` + unit-тесты инструментов.
6. **Packaging/docs**: pyproject extra `mcp` + entry point; `connections/example.yaml`;
   README (раздел MCP + «добавление нового движка»); AGENTS.md.
7. **Integration**: testcontainers-PG — run_query/explain/RO-транзакция e2e.

## Проверки (acceptance)

- `uv run ruff check .` — чисто.
- `uv run pytest` — unit зелёные; `uv run pytest -m integration` — MCP-e2e зелёные (Docker).
- Ручная проверка: `uv run db-pm-mcp` стартует, отвечает на MCP-рукопожатие
  (проверка через MCP-клиент ZCode).

## Риски

- **Долгие deploy-операции vs таймауты MCP-клиентов** — прогресс собирается в
  ответ; в README предупреждение о настройке таймаута клиента.
- **GP 6 и `EXPLAIN (FORMAT JSON)`** — fallback на text при ошибке.
- **psycopg2 multi-statement + RO-обёртка** — `BEGIN READ ONLY` / `ROLLBACK`
  только вокруг одиночного стейтмента `query`; `run_script` идёт по существующему
  пути `execute_script` (AUTOCOMMIT), классификация до исполнения.

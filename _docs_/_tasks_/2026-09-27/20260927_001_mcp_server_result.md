# Phase 19 — MCP Server: результат (result)

> Контекст:
> - план: `_tasks_/2026-09-27/20260927_001_mcp_server_plan.md`
> - user stories: `_tasks_/ROADMAP.md` §7 (MCP-1..MCP-9)
> - референс: crystaldba/postgres-mcp (заимствования см. в плане §Решения)

**Дата:** 2026-09-27
**Статус:** реализовано в полном объёме MCP-1..MCP-9; живая проверка на
локальном PostgreSQL 18 (read-only smoke + stdio-рукопожатие MCP-клиентом).

## Что сделано

| Слой | Файл | Что |
|------|------|-----|
| Domain | `domain/query.py` | `QueryResult`, `ExplainResult` — диалект-агностичные |
| Domain | `domain/connection.py` | `McpSettings` (allow_writes, allow_deploy, row_limit, query_timeout_s) + `ConnectionConfig.mcp_settings` |
| Infrastructure | `infrastructure/database/base.py` | MCP-секция контракта: `run_query`/`explain` (abstract), `get_top_queries` (NotSupportedError по умолчанию), `NotSupportedError`, capability-флаги, рецепт расширения на новый движок |
| Infrastructure | `postgres/adapter.py` | `run_query` (read-only транзакция backstop, statement_timeout, truncation, JSON-сериализация значений), `explain` (text/json/auto + fallback на text, ANALYZE в RO-транзакции), `get_top_queries` (pg_stat_statements, probe modern/legacy колонок PG13+/GP6) |
| Infrastructure | `postgres/queries.py` | `GET_TOP_QUERIES_MODERN/LEGACY` |
| Infrastructure | `infrastructure/sql/classify.py` | sqlglot-классификатор: READ_ONLY/WRITE/DESTRUCTIVE/UNKNOWN, denylist функций, FOR UPDATE/SHARE, COMMIT/ROLLBACK, диалект-мапа `DB_TYPE_DIALECT` |
| Application | `application/mcp_service.py` | `ConnectionManager` (кэш адаптеров + Lock на адаптер), `MCPQueryService` (policy-гейты), `extract_*` фильтры структуры |
| Presentation | `presentation/mcp/` (`main.py`, `server.py`, `tools.py`) | entry point `db-pm-mcp` (typer, bootstrap-only аргументы), FastMCP stdio, 12 инструментов с аннотациями readOnly/destructive и LLM-описаниями |
| Packaging | `pyproject.toml` | optional-dependencies `mcp = ["mcp>=1.2,<2"]`, script `db-pm-mcp`, dev-группа дублирует mcp (тесты регистрации под `uv run pytest`) |
| Docs | README (раздел MCP), AGENTS.md, `connections/example.yaml` | запуск, конфиги ZCode/Claude Desktop, модель безопасности, рецепт нового движка, блок `mcp:` |
| Tests | `test_sql_classify.py` (61), `test_mcp_service.py` (23), `test_mcp_tools.py` (13), `test_mcp_query_integration.py` (16, marker integration) | классификатор, policy, инструменты, e2e-адаптер |

## Ключевые решения сессии

- **RO-backstop на отдельном pooled-соединении**: `run_query` берёт
  connection из engine (не общий `self._connection`) — `SET statement_timeout`
  и read-only транзакция не протекают в деплой-операции; `RESET
  statement_timeout` в конце.
- **EXPLAIN всегда в read-only транзакции**: план без ANALYZ не исполняет
  ничего (RO безвреден), ANALYZE write-стейтмента блокируется сервером —
  второй рубеж после классификатора.
- **`fmt="auto"` = JSON с fallback на text**: Greenplum 6 может не
  сериализовать свои plan-nodes (Motion, ShareInputScan) в JSON — fallback
  возвращает text-план с заметкой.
- **pg_stat_statements probe кэшируется по соединению** (паттерн
  pg_sequence/prokind): PG 13+ `total_exec_time` vs GP 6 `total_time`;
  отсутствие расширения → `NotSupportedError` (soft degradation).
- **Деплой-инструменты повторяют проводку CLI 1-в-1** (сервисы +
  `create_run_dir` + progress → последние 50 строк в ответе) — MCP не может
  обойти rehearsal/Safety Gate/`allow_drop_schemas`; подтверждение сброса —
  параметр `confirm_database` (аналог ввода имени БД в CLI).
- **Вся политика в `connections/*.yaml`** (блок `mcp:`) — сервер не имеет
  поведенческих флагов; `list_connections` отдаёт эффективные права, чтобы
  LLM видел границы до вызова.

## Живая проверка (без Docker — решение пользователя, см. 20260925_001)

1. **Адаптер × реальный PG 18** (`local-PG-18…`, БД `postgres`):
   `run_query` SELECT ✓; `CREATE TABLE` в readonly → `ReadOnlySqlTransaction`
   ✓ (backstop работает); `explain` text/JSON/ANALYZE ✓;
   `get_top_queries` → корректный NotSupportedError (нет расширения) ✓.
2. **MCP-протокол e2e**: `uv run db-pm-mcp` — stdio JSON-RPC: initialize ✓,
   tools/list → все 12 ✓, tools/call `list_connections` → реальные
   подключения ✓ (без секретов, с правами).
3. **Unit**: `uv run pytest` — 1160 passed; `uv run ruff check .` — чисто.

## Инцидент окружения (исправлен)

`uv run pytest` молча использовал python из `D:\repos\...\db-project-manager`
(venv был создан до переноса репо на `C:`; shebang'и .exe в Scripts указывали
на старый путь, где осталась копия venv). Диагностика: `sys.executable` внутри
pytest ≠ `uv run python`. Лечение: `.venv` пересоздан (`rm -rf .venv && uv sync
--extra mcp`). Урок — LESSONS_LEARNED §75.

## Известные ограничения / NOT done

- `list_objects`/`get_object_details` читают полную структуру
  (`get_database_structure()`) и фильтруют — на очень больших БД дорого;
  гранулярные методы контракта — кандидат в будущую фазу.
- `run_script` исполняется целиком в AUTOCOMMIT (без по-стейтментной
  транзакции) — сознательно, в соответствии с моделью деплоя проекта.
- EXPLAIN ANALYZE write-стейтментов в write-режиме недоступен (RO-обёртка
  объясняет всегда) — компенсируется `explain analyze` после pre-скриптов.
- Health-checks (PgHero-стиль), hypopg-тюнинг индексов, SSE-транспорт —
  осознанно вне фазы (см. план §Решения-8).
- MCP-прогресс не стримится (не.notifications) — хвост прогресса в ответе.
- `get_top_queries` на GP 6 не проверен живым кластером (запрос с legacy-колонками
  написан по документации 9.4; probe выбирает набор колонок автоматически).

## Коммиты

- `a3ae30a` docs(roadmap) — Phase 19, направление D
- `d9e42ba` docs(tasks) — план
- `8e56093` feat(adapter) — MCP-секция контракта + PG-реализация + McpSettings
- `977b598` feat(sql) — классификатор
- `af05d27` feat(app) — ConnectionManager + MCPQueryService
- `0b97f2c` feat(mcp) — presentation/mcp + pyproject extra
- `0323d2e` chore(lint) — misc/ скрипты
- `547fe62` test(integration) — MCP e2e
- далее — docs(readme), docs(tasks), docs(checkpoint), docs(lessons)

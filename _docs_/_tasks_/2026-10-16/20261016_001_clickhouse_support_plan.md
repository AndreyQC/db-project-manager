# Поддержка Clickhouse: доступ, RE, deploy, validate (plan)

> **Date:** 2026-10-16
> **Status:** в работе

> Контекст:
> - `_docs_/_checkpoints_/20261011_001_checkpoint.md` — текущее состояние (Phase 20 завершена)
> - `src/db_project_manager/infrastructure/database/base.py` — контракт `DatabaseAdapter`
> - `src/db_project_manager/infrastructure/database/registry.py` — регистр адаптеров
> - `src/db_project_manager/infrastructure/database/postgres/adapter.py` — реализация PG/GP
> - `src/db_project_manager/infrastructure/yaml_project/column_parsers.py` — парсер колонок Clickhouse (уже есть, `dialect="clickhouse"`)

## Цель

Добавить в db-pm полноценную поддержку Clickhouse как целевой СУБД:
подключение, reverse engineering схемы, deploy apply, deploy validate.

## Зачем

Clickhouse — аналитическая СУБД, всё чаще используется как основное
хранилище. Текущая кодовая база работает только с PostgreSQL/Greenplum.
Поддержка Clickhouse позволит:
- управлять схемами Clickhouse аналогично PG/GP;
- использовать существующие механизмы deploy/safety-gate/profiling;
- обеспечить единый workflow для гибридной инфраструктуры.

## Решение

### 1. Доменные изменения

**`domain/connection.py`** — добавить `"clickhouse"` в `SUPPORTED_DB_TYPES`:
```python
"clickhouse": "ClickHouse",
```

**Новый файл `infrastructure/database/clickhouse/`**:
- `__init__.py`
- `adapter.py` — `CHDatabaseAdapter(DatabaseAdapter)`
- `queries.py` — Clickhouse-specific SQL-queries

### 2. Clickhouse-адаптер (adapter.py)

Ключевые отличия от PG/GP:

| Контракт | PG/GP | Clickhouse | Примечание |
|----------|-------|------------|------------|
| Схемы | `CREATE SCHEMA` | `CREATE DATABASE` | В CH нет схем в PG-смысле; «база» = namespace |
| Таблицы | `pg_class` | `system.tables` | Типы: `MergeTree`, `Log`, `Memory`, etc. |
| Колонки | `information_schema.columns` | `system.columns` | Типы данных отличаются |
| Функции | `pg_proc` | `system.functions` / `system.table_functions` | UDF, lambda |
| Констрейнты | `information_schema.table_constraints` | Нет (CK нет) | Primary key = `ORDER BY` в engine |
| Индексы | `pg_index` | `skip index` (DataSkippingIndex) | Отдельный тип |
| Материализованные представления | `pg_matviews` | `system.tables WHERE engine = 'MaterializedView'` | |
| Внешние таблицы | `pg_foreign_table` | `system.tables WHERE engine LIKE '%Table'` | S3, ODBC, MySQL, etc. |

**Драйвер:** `clickhouse-connect` (async-опционально, sync достаточно для RE/deploy).

**ConnectionConfig** для Clickhouse:
- `host`, `port` (default 8443 для TLS, 8123 для HTTP)
- `database` (аналог схемы)
- `username`, `password`
- `secure` (TLS)
- `clickhouse_database` — эквивалент схемы (в CH «database»)

### 3. Реализация контракта

```python
class CHDatabaseAdapter(DatabaseAdapter):
    supports_readonly_txn = False   # CH не имеет read-only транзакций в PG-стиле
    supports_statement_timeout = False

    def connect(self, cfg: ConnectionConfig) -> None:
        import clickhouse_connect
        client = clickhouse_connect.get_client(
            host=cfg.host, port=cfg.port or 8443,
            username=cfg.username, password=cfg.password,
            database=cfg.database,
            secure=cfg.secure,
        )

    def get_database_structure(self) -> dict[str, Any]:
        # system.tables → databases (вместо schemas), tables, views, mv, dicts
        # system.columns → колонки
        # system.functions → функции
        # system.table_functions → table functions
        # skip indexes (DataSkippingIndex)
        pass

    def check_can_create_db(self) -> bool:
        # CH: проверяем право на CREATE DATABASE
        pass

    def get_server_timestamp_utc(self) -> str:
        # SELECT nowUTC() → format
        pass

    def create_database(self, name: str, ...) -> None:
        # CREATE DATABASE IF NOT EXISTS <name>
        pass

    def drop_database(self, name: str) -> None:
        # DROP DATABASE IF EXISTS <name>
        pass

    def execute_script(self, script: str) -> None:
        # client.command(script)
        pass

    # Phase 9, 11: релевантно для Clickhouse
    def get_table_row_counts(self) -> list[dict]:
        # system.tables where database = <db> → rows
        pass

    def get_table_presence_stats(self) -> list[TablePresenceStats]:
        # аналогично, rows + freshness
        pass

    # Phase 10: __deploy schema
    # В CH сервисная БД = __deploy (отдельная БД, не схема)
    def get_schema_version(self, schema_name: str) -> str | None:
        # SELECT MAX(version) FROM __deploy.schema_version WHERE deployed_to = '<schema>'
        pass

    def record_schema_version(self, ...) -> None:
        # INSERT INTO __deploy.schema_version
        pass

    def get_script_history(...) -> ScriptRecord | None:
        pass

    def record_script_execution(...) -> None:
        pass

    # Phase 18: deploy reset
    def list_schemas(self) -> list[str]:
        # system.databases WHERE name NOT IN ('system', 'INFORMATION_SCHEMA')
        pass

    def get_schema_object_counts(self) -> dict[str, int]:
        pass

    def drop_schema(self, name: str) -> None:
        # DROP DATABASE <name>
        pass

    def drop_schema_contents(self, schema: str) -> None:
        # DROP TABLE <db>.<table>; ... для всех таблиц, MV, словарей
        pass

    def snapshot_schema_acls(self, schemas: list[str]) -> str:
        # CH не имеет ACL в PG-стиле; возвращать пустую строку
        pass

    def truncate_table(self, schema: str, name: str) -> None:
        # ALTER TABLE <db>.<name> DELETE WHERE 1=1 (или TRUNCATE для движков)
        pass

    def drop_extension(self, name: str) -> None:
        # CH не имеет расширений; no-op
        pass

    def list_extensions(self) -> list[dict]:
        return []  # CH не имеет extension-концепции

    # Phase 19: MCP
    def run_query(self, ...) -> QueryResult:
        # client.query(...) → rows
        pass

    def explain(self, ...) -> ExplainResult:
        # EXPLAIN (отсутствует в CH) → NotSupportedError
        pass

    def get_top_queries(self, ...) -> list[dict]:
        # system.query_log (если enabled)
        pass
```

### 4. Reverse Engineering (RE) для Clickhouse

RE в Clickhouse специфичен из-за отсутствия классических схем:

- **Database** = namespace (аналог схемы в PG)
- **Таблицы** — движок (MergeTree, Log, Memory, etc.), `ORDER BY`, `PARTITION BY`, `PRIMARY KEY`, TTL
- **Колонки** — тип, default выражение (MATERIALIZED, EPHEMERAL, ALIAS)
- **Словари** (dictionaries) — внешние key-value хранилища
- **Материализованные представления** — хранят данные + SELECT
- **Внешние таблицы** (table engines) — S3, ODBC, MySQL, PostgreSQL, MongoDB
- **Функции** — встроенные + lambda
- **UDF** — пользовательские функции (если есть)

**snapshot.py** для Clickhouse должен корректно:
1. Читать движок таблицы
2. Парсить `ORDER BY`, `PARTITION BY`, `PRIMARY KEY` (ключ сортировки MergeTree)
3. Парсить TTL
4. Парсить колонки с MATERIALIZED/EPHEMERAL/ALIAS (уже есть парсер в `column_parsers.py`)

### 5. Deploy для Clickhouse

Clickhouse DDL отличается от PostgreSQL:
```sql
-- Таблица
CREATE TABLE db.table (
    id UInt64,
    name String,
    created DateTime DEFAULT now()
) ENGINE = MergeTree()
ORDER BY id
PARTITION BY toYYYYMM(created)
TTL created + INTERVAL 1 YEAR;

-- Материализованное представление
CREATE MATERIALIZED VIEW db.mv
ENGINE = SummingMergeTree()
ORDER BY (id, date)
AS SELECT ...
```

**deploy_apply_service.py** должен:
- Обрабатывать `CHDatabaseAdapter` отдельно для deploy flow
- Использовать `IF NOT EXISTS` / `IF EXISTS` для idempotency

### 6. Deploy Validate

- Создание временной БД `_<original>_validate_<timestamp>`
- Выполнение скриптов
- Проверка результата (таблицы созданы, данные на месте)
- Удаление временной БД

### 7. SQL классификация (для deploy apply)

**`infrastructure/sql/classify.py`** — добавить `clickhouse` в `DB_TYPE_DIALECT`:
```python
"clickhouse": "clickhouse",
```

### 8. MCP-сервер (Phase 19)

MCP-сервер `db-pm-mcp` уже реализован для PG/GP. Для Clickhouse
необходима адаптация следующих инструментов:

**Существующие MCP-инструменты и их адаптация:**

| Инструмент | PG/GP | Clickhouse | Примечание |
|------------|-------|------------|------------|
| `mcp_query` | `run_query` | `run_query` | query/command/query_df — базовая функция |
| `mcp_explain` | `EXPLAIN (json/text)` | **N/A** | CH не имеет EXPLAIN в PG-стиле → `NotSupportedError` |
| `mcp_list_schemas` | `pg_namespace` | `system.databases` | |
| `mcp_list_tables` | `pg_class` | `system.tables` | |
| `mcp_describe_table` | `information_schema` | `system.columns` + `system.tables` | нужно JOIN для движка |
| `mcp_list_views` | `pg_views` | `system.tables WHERE engine LIKE 'View'` | |
| `mcp_list_materialized` | `pg_matviews` | `system.tables WHERE engine LIKE 'Materialized%'` | |
| `mcp_list_functions` | `pg_proc` | `system.functions` | |
| `mcp_list_columns` | `information_schema.columns` | `system.columns` | |
| `mcp_get_indexes` | `pg_indexes` | `system.table_engines` / skip indexes | |
| `mcp_table_size` | `pg_total_relation_size` | `system.parts` (по database/table) | |
| `mcp_profile_table` | `pg_stat_user_tables` | `system.metrics` / `system.query_log` | |
| `mcp_top_queries` | `pg_stat_statements` | `system.query_log` (если enabled) | |

**CH-специфичные инструменты (новые):**

| Инструмент | Описание |
|------------|----------|
| `mcp_ch_list_mergetree` | Список MergeTree-таблиц с ORDER BY, PARTITION BY |
| `mcp_ch_list_dictionaries` | Список словарей (system.dictionaries) |
| `mcp_ch_list_engines` | Доступные движки таблиц |
| `mcp_ch_list_parts` | Partition parts (system.parts) |
| `mcp_ch_list Mutations` | Список мутаций (system.mutations) |

**Адаптация `run_query` для Clickhouse:**

```python
def run_query(self, sql: str, *, max_rows: int = 50, ...) -> QueryResult:
    # clickhouse_connect возвращает QueryResult
    result = self.client.query(sql)
    rows = result.result_rows
    columns = [col.name for col in result.columns]
    
    return QueryResult(
        columns=columns,
        rows=rows[:max_rows],
        truncated=len(rows) > max_rows,
        row_count=len(rows),
    )
```

**QueryResult.notices** для CH:
- При отсутствии `system.query_log` → `"CH: query_log disabled, performance analysis unavailable"`

## Проверки

- [ ] Подключение к Clickhouse-базе через CLI/GUI
- [ ] RE: корректный `ObjectSnapshot` для таблиц Clickhouse (движок, ORDER BY, TTL)
- [ ] RE: MV, словари, функции
- [ ] `deploy apply` — создание таблицы, MV
- [ ] `deploy validate` — временная БД, apply, verify, cleanup
- [ ] `db-pm graph build` с Clickhouse-подключением
- [ ] Юнит-тесты (mock клиента, как для PG)
- [ ] `uv run pytest` — всё проходит

## Риски / ограничения

1. **Нет транзакций в PG-стиле** — deploy apply не сможет rollback на mid-deploy failure; пользователь должен быть готов к partial state. Документировать.
2. **Нет CONSTRAINT** — safety gate SG-1 (not null, check, unique, fk) не применим к Clickhouse; SG пропускает эти правила.
3. **TTL ≠ срок хранения** — при RE TTL попадает в снапшот, но deploy должен аккуратно пересоздавать (TTL входит в `CREATE TABLE`).
4. **Внешние таблицы** — движки S3/ODBC/etc. требуют дополнительных параметров (credentials, DSN); RE должен их захватывать.
5. **Нет схем в PG-смысле** — «database» = namespace, но db-pm проект может содержать код для нескольких БД; логика `list_schemas()` и deploy reset должны работать с базами.
6. **clickhouse-connect** — sync-драйвер; для async-операций (GUI progress) потребует threading.

## Следующие шаги (sub-tasks)

1. **20261016_001a** — Добавить `CHDatabaseAdapter` в registry + ConnectionConfig
2. **20261016_001b** — Реализовать `get_database_structure()` (RE core)
3. **20261016_001c** — Адаптер: Phase 2 (validation-deploy), Phase 9 (row counts)
4. **20261016_001d** — Адаптер: Phase 10 (__deploy), Phase 18 (reset)
5. **20261016_001e** — Поддержка в deploy apply / delta service
6. **20261016_001f** — MCP-сервер: адаптация 13+ инструментов + CH-специфичные
7. **20261016_001g** — Юнит-тесты + интеграционные (если Docker с CH)

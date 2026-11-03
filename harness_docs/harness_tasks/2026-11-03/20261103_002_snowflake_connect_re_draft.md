# Phase 21 — Snowflake: подключение (браузерная SSO) + reverse-engineering (draft)

> Контекст:
> - `harness_checkpoints/20261103_001_checkpoint.md` — текущий контекст проекта.
> - `infrastructure/database/base.py` — контракт `DatabaseAdapter` и рецепт
>   добавления нового DBMS (docstring, «Phase 19 extension recipe»).
> - `infrastructure/database/clickhouse/adapter.py` — шаблон адаптера для
>   движка без полного деплой-сурфейса (stub через `NotSupportedError`).
> - `TASK_CONVENTIONS.md` §2.2 — цикл draft → final.

**Дата:** 2026-11-03 (создано)
**Статус:** Draft — 4 `USER_INPUT` закрыть диалогом, затем `_plan.md`

---

## Цель фазы

Подключение к Snowflake с регистрацией через браузер (SSO externalbrowser)
и выполнение reverse-engineering: кодовая база `.sql` + `.dbm_graph` из
живого аккаунта Snowflake. Деплой в Snowflake — **вне этой фазы**
(следующая; адаптер оставляет деплой-сурфейс в stub).

## Что уже готово под Snowflake (замер 2026-11-03)

- `domain/connection.py:151` — docstring поля `type` уже называет
  `snowflake` (значение валидно пройдёт в модель, упадёт только registry).
- `infrastructure/sql/classify.py:86-93` — `DB_TYPE_DIALECT["snowflake"] =
  "snowflake"`: sqlglot-классификация (MCP classifier) готова.
- `infrastructure/database/base.py:34-44` — рецепт добавления DBMS:
  пакет + registry + `SUPPORTED_DB_TYPES` + pyproject extra + диалект.
- `ReverseEngineerService` движок-агностик: строит кодовую базу из
  `adapter.get_database_structure()` (форма `{"schemas": [...]}`), рендер
  autodoc/manifest общий.
- Крипта: `crypto__<ENV>__<token>` в connections/*.yaml работает для любого
  строкового поля, расшифровка на стороне `ConnectionStore`.
- sqlglot умеет диалект `snowflake` (normalize/classify из коробки).

## Архитектура (по рецепту Phase 19)

1. **Пакет** `infrastructure/database/snowflake/`: `adapter.py`
   (`SnowflakeAdapter(DatabaseAdapter)`) + `queries.py` (SQL к
   INFORMATION_SCHEMA/SHOW/GET_DDL) + `__init__.py`.
2. **Registry**: `registry.py` — `if db_type == "snowflake": return
   SnowflakeAdapter()`; текст ошибки — дополнить списком.
3. **Domain**: `SUPPORTED_DB_TYPES` += `"snowflake"`.
4. **pyproject**: extra `snowflake = ["snowflake-connector-python>=3,<4",
   "snowflake-connector-python[secure-local-storage]"]` — второй нужен для
   кэша SSO-токена (keyring/WAM), см. «Аутентификация».
5. **Парсер**: отдельный парсер не нужен — `PgSqlParser` (word-scan)
   обрабатывает DDL; риски синтаксиса — см. «Риски».

## Аутентификация

**Основной сценарий (запрошен пользователем) — externalbrowser:**
`snowflake.connector.connect(..., authenticator="externalbrowser")` —
драйвер открывает браузер по умолчанию, пользователь проходит SSO
(Okta/AAD/…), драйвер дожидается токена. Регистрация в браузере —
один раз на срок жизни кэша.

**Кэш токена** (`client_store_temporary_credential=True`): после первого
браузерного входа токен хранится в OS-хранилище (Windows: Credential
Manager/WAM) — повторные подключения без браузера. Требует extra
`secure-local-storage` (keyring).

**Маппинг конфига** (без ломки доменной модели):
- `host` = `<account>.snowflakecomputing.com` (account identifier
  извлекается из хоста), `port` = 443, `database` = база;
- `options:` (yaml) — `warehouse`, `role`, `schema` (default context),
  `authenticator` (`externalbrowser` | `snowflake`), 
  `client_store_temporary_credential: true`;
- `username` — логин SSO; `password` — только для fallback-аутентификации
  `authenticator: snowflake` (крипта `crypto__…` работает как для PG).

**USER_INPUT-1 — набор методов аутентификации MVP:**
- (а) externalbrowser + пароль как fallback — РЕКОМЕНДАЦИЯ: браузер —
  целевой сценарий пользователя, пароль почти бесплатен (поле есть,
  крипта есть), закрывает headless-серверы, где браузера нет;
- (b) только externalbrowser — минимальный сурфейс, headless не работает;
- (c) externalbrowser + пароль + key-pair — key-pair (private key файл)
  полезен для сервисных аккаунтов, но +пол-этапа (генерация/парсинг
  ключа, шифрование ключа) — предлагаю отложить в BACKLOG.

## Reverse-engineering: источник метаданных

- Перечень объектов: `INFORMATION_SCHEMA.<TABLES|VIEWS|…>` текущей базы
  (быстро, не зависит от latency ACCOUNT_USAGE) + `SHOW OBJECTS IN
  DATABASE` там, где INFORMATION_SCHEMA не покрывает.
- DDL каждого объекта: `SELECT GET_DDL('TABLE', 'db.schema.name')` —
  аналог pg_get_indexdef/pg_get_viewdef, рендерит полный CREATE.
- Форма `get_database_structure()` — та же `{"schemas": [{name, comment,
  tables, views, materialized_views, sequences, functions, procedures,
  indexes: [], enums: []}]}` — рендер кодовой базы не меняется.

**USER_INPUT-2 — состав объектов RE MVP:**
- (а) базовые: tables (с constraint), views, materialized_views,
  sequences — РЕКОМЕНДАЦИЯ: покрывает основной кейс, GET_DDL для них
  стабилен;
- (b) (а) + functions/procedures — паритет с PG-RE; но тела могут быть
  JS/Python/Snowflake-scripting — word-scan парсера по ним бесполезен,
  рёбра не строятся (объект будет «кубом» без зависимостей — это ок);
- (c) (а) + external/dynamic tables, streams, tasks — специфика Snowflake,
  отдельные типы в графе (расширение `SUPPORTED_TYPES` и рендера);
  предлагаю этапом 2 по запросу.

## Этапы фазы (предварительно)

- **S1. Адаптер + подключение**: пакет, registry, domain, extra; `connect`
  с externalbrowser + кэшем токена; CLI-смоук `db-pm query`-путь
  (`run_query`-заглушка или минимальный SELECT 1 через cursor); unit-тесты
  на моках connector-модуля.
- **S2. Структура + RE**: `get_database_structure` (INFORMATION_SCHEMA +
  GET_DDL), состав по USER_INPUT-2; live-RE на тестовой базе → кодовая
  база + manifest; unit на фикстуре-структуре.
- **S3. Граф**: `graph build`/`graph validate` на Snowflake-кодовой базе;
  правки word-scan, если Snowflake-синтаксис роняет разбор (см. Риски);
  `deploy validate` — ожидаемо «движок не поддерживает» (stub) —
  убедиться в graceful-сообщении.
- **S4 (опционально, по USER_INPUT-3/4)**: GUI-диалог (тип snowflake в
  списке, browser-auth без пароля), MCP `run_query`/`explain` для
  Snowflake (диалект sqlglot уже есть).
- **S5. Доки**: result, README («Возможности»), LESSONS если вылезут.

## Проверки

- Unit: моки `snowflake.connector` (пакет в dev-зависимостях — нет, мокать
  импорт; драйвер в extra, в CI не ставится) — тесты адаптера не должны
  требовать установки драйвера (ленивый import внутри `connect`, как
  принято для CH/PG драйверов).
- Live-смоук (пользователь, тестовый аккаунт Snowflake): браузерная
  регистрация → кэш токена → RE → `graph build` → `graph validate`.
  **Docker/integration-контейнеров для Snowflake нет — live-проверка
  только руками пользователя** (как CH-проверки 2026-10-16).
- Регресс: полный `uv run pytest` (1343), ruff; PG/GP/CH не затронуты
  (registry/dispatch — покрыть тестом, что старые типы не сломались).

## Риски

1. **Парсер**: Snowflake-специфика (`QUALIFY`, `::`, `ILIKE`, stage-ссылки
   `@…`, `TRANSFORM`) в телах вью/функций — word-scan может строить ложные
   рёбра или падать. Смягчение: sqlglot-normalize (как для CH canonical_ddl),
   точечные фиксы `pg_sql_parser` по живым примерам; UDF-тела на JS/Python —
   `build: false` или исключение из parse.
2. **GET_DDL** не поддерживает dynamic/external-таблицы в старых версиях
   (`GET_DDL` c `SHOW OBJECTS`-обходом) — зависит от USER_INPUT-2c.
3. **Кэш токена** требует OS-хранилище — на «чистых» Linux-серверах без
   keyring backend fallback: браузер на каждое подключение (или пароль).
4. **Нет контейнера** — integration-тестов в CI не будет, только live у
   пользователя; качество обеспечивается unit-ами на моках + фиксацией
   реальных DDL в фикстурах.
5. **Деплой-сурфейс**: 15+ абстрактных методов контракта — реализуются
   stub-ами (`NotSupportedError`) по образцу CH; риск «случайно вызвать»
   закрывается graceful-сообщениями (паттерн Phase 19 уже отработан).

**USER_INPUT-3 — границы фазы:** (а) connect + RE + graph — РЕКОМЕНДАЦИЯ;
(b) + compare между Snowflake и чем-либо — сравнение требует обоих
адаптеров с полным сурфейсом, предлагаю отдельно; (в) сразу + deploy
validate — большой кусок (временные БД в Snowflake — это отдельные
database/schema-клоны), отдельная фаза.

**USER_INPUT-4 — GUI:** (а) CLI-only MVP, GUI-диалог — этапом после
live-проверок — РЕКОМЕНДАЦИЯ; (b) GUI сразу в фазе.

---

## Открытые вопросы (USER_INPUT)

| # | Вопрос | Рекомендация |
|---|---|---|
| 1 | Методы аутентификации MVP | (а) externalbrowser + пароль-fallback |
| 2 | Состав объектов RE | (а) tables/views/mviews/sequences |
| 3 | Границы фазы | (а) connect + RE + graph |
| 4 | GUI в фазе | (а) CLI-only MVP |

---

*Файл: `harness_docs/harness_tasks/2026-11-03/20261103_002_snowflake_connect_re_draft.md`*

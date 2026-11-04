# Phase 21 — Snowflake: подключение (браузерная SSO) + reverse-engineering (plan)

> Контекст:
> - `20261103_002_snowflake_connect_re_draft.md` — проработка, замер готовности,
>   риски (draft остаётся для истории).
> - `harness_checkpoints/20261103_001_checkpoint.md` — контекст проекта.
> - `infrastructure/database/base.py:34-44` — рецепт добавления DBMS;
>   `infrastructure/database/clickhouse/adapter.py` — шаблон stub-адаптера.

**Дата:** 2026-11-03
**Статус:** План согласован (USER_INPUT закрыты диалогом 2026-11-03)

---

## Решения (USER_INPUT → финал)

| # | Вопрос | Решение |
|---|---|---|
| 1 | Аутентификация MVP | **externalbrowser (браузерная SSO-регистрация) + пароль как fallback**. Key-pair — вне фазы, в BACKLOG (P3) |
| 2 | Состав объектов RE | **Базовые (tables с constraint, views, materialized_views, sequences) + functions/procedures** (паритет с PG-RE). External/dynamic tables, streams, tasks — вне MVP, по запросу |
| 3 | Границы фазы | **connect + RE + graph build/validate**. Деплой-сурфейс — stub (`NotSupportedError`), деплой в Snowflake — отдельная следующая фаза |
| 4 | GUI | **GUI-диалог подключения делаем в этой фазе** (тип snowflake, browser-auth), не откладываем |

Нумерация: Phase 21 — следующая свободная (Phase 20 в коде занята
table-profiling: `domain/profiling.py`, `ProfilingSettings`).

## Цели фазы

1. Подключение к Snowflake с регистрацией через браузер: первый вход —
   браузер SSO, дальше — кэш токена в OS-хранилище (без браузера).
2. RE: кодовая база `.sql` (schema/types/*.sql + autodoc) + manifest из
   живого аккаунта — tables/views/mviews/sequences/functions/procedures.
3. Граф: `graph build` / `graph validate` на Snowflake-кодовой базе.
4. GUI: тип snowflake в диалоге подключения, авторизация через браузер.
5. Деплой-сурфейс адаптера — stub с graceful-сообщениями (паттерн CH,
   `NotSupportedError`).

## Архитектура (рецепт Phase 19)

- `infrastructure/database/snowflake/` — `adapter.py` + `queries.py`
  (`INFORMATION_SCHEMA` перечни + `GET_DDL` для тел);
  форма `get_database_structure()` — без изменений
  (`{"schemas": [...]}`), рендер RE общий.
- `registry.py` — dispatch `snowflake`; `SUPPORTED_DB_TYPES` +=
  `snowflake` (docstring поля уже называет его).
- pyproject: extra `snowflake = ["snowflake-connector-python>=3,<4",
  "snowflake-connector-python[secure-local-storage]"]` (второй — кэш
  SSO-токена через keyring/WAM).
- Диалект sqlglot уже готов: `DB_TYPE_DIALECT["snowflake"]`.
- Конфиг без ломки модели: `host=<account>.snowflakecomputing.com`,
  `port=443`; в `options:` yaml — `warehouse`, `role`, `schema`,
  `authenticator` (`externalbrowser` | `snowflake`),
  `client_store_temporary_credential: true`. Пароль — fallback
  `authenticator: snowflake`, крипта `crypto__…` работает как для PG.
- GUI: диалог подключения — добавить тип в список, для browser-auth
  пароль не обязателен; кнопка «проверить подключение» дожидается
  браузерного входа (таймаут и подсказка пользователю).

## Этапы

- **S1. Адаптер + подключение**: пакет/registry/domain/extra; `connect`
  externalbrowser + кэш токена + пароль-fallback; ленивый import драйвера
  (unit-тесты без установки extra); unit на моках connector-модуля.
- **S2. Структура + RE**: `get_database_structure` — базовые типы +
  functions/procedures (`GET_DDL`); live-RE на тестовой базе; unit на
  зафиксированной структуре-фикстуре.
- **S3. Граф**: `graph build`/`validate` на Snowflake-кодовой базе;
  точечные правки word-scan по живым примерам синтаксиса; graceful-
  сообщение при попытке `deploy validate`.
- **S4. GUI**: тип snowflake в диалоге, browser-auth, проверка подключения.
- **S5. Доки**: result, README «Возможности», LESSONS; BACKLOG-запись на
  key-pair (сделана при коммите плана).

## Проверки

- `uv run pytest` — все существующие + новые unit (моки, ленивый import,
  registry-регресс старых типов), ruff.
- Live-смоук (пользователь, тестовый аккаунт): браузерная регистрация →
  кэш токена (повторное подключение без браузера) → RE → `graph build`
  → `graph validate` → GUI-подключение.
- Контейнера Snowflake нет — integration в CI невозможен; качество:
  unit на моках + реальные DDL в фикстурах (как CH 2026-10-16).

## Риски (из draft, релевантные принятым решениям)

1. Word-scan парсера на Snowflake-синтаксисе (`QUALIFY`, `::`, stage-ссылки)
   — точечные фиксы по живым DDL; UDF-тела на JS/Python рёбер не дают (ок).
2. Кэш токена требует OS-хранилище; headless-Linux fallback — пароль или
   браузер на каждое подключение.
3. GET_DDL покрывает все типы MVP-состава (functions/procedures включительно);
   всё вне состава — осознанно не трогаем.
4. Драйвер тяжёлый (snowflake-connector-python) — только extra, в базовые
   зависимости не попадает.

## OUT of scope (следующие фазы / по запросу)

- Деплой в Snowflake (validate/apply/reset) — отдельная фаза.
- key-pair аутентификация — BACKLOG P3.
- External/dynamic tables, streams, tasks, stages — по запросу.
- MCP-инструменты для Snowflake (`run_query`/`explain`) — сурфейс готов
  под них (диалект есть), приделать после live-проверок.

---

*Файл: `harness_docs/harness_tasks/2026-11-03/20261103_002_snowflake_connect_re_plan.md`*

# Phase 21 — Snowflake: подключение (браузерная SSO) + reverse-engineering (result)

> Контекст:
> - `20261103_002_snowflake_connect_re_plan.md` — согласованный план (решения).
> - `20261103_002_snowflake_connect_re_draft.md` — проработка и замер.
> - `harness_checkpoints/20261103_001_checkpoint.md` — контекст проекта.

**Дата:** 2026-11-03
**Статус:** Инструментальная часть выполнена (S1–S4); live-смоук — за
пользователем (нужен тестовый аккаунт Snowflake)

---

## Что сделано (коммиты)

| Этап | Коммит | Содержание |
|---|---|---|
| S1 | `1f80619` | Каркас: `infrastructure/database/snowflake/` (adapter+queries), registry, `SUPPORTED_DB_TYPES`, extra `snowflake` (драйвер + secure-local-storage/keyring); connect: externalbrowser (браузерная SSO, кэш токена в OS-хранилище) + пароль-fallback; `run_query` (read-сурфейс); деплой/reset/safety — stub с graceful-сообщениями |
| S2 | `0af6e1d` | `get_database_structure`: SHOW (перечни+комментарии+текст вью) + INFORMATION_SCHEMA (колонки/constraints, bind-параметры) + GET_DDL (тела рутийн); constraints-definitions рендерятся в адаптере; шаблоны matview/sequence — conditionals (назад-совместимо); RE e2e на фикстурах |
| S3-пре | `5677035` | **Case-insensitive lookup имён в word-scan** (функциональный фикс, см. ниже); граф на RE-кодовой базе (11 вершин, рёбра, порядок без циклов); пробник синтаксиса QUALIFY/::/ILIKE/FLATTEN/VARIANT/AUTOINCREMENT/$$-скриптинг/@stage — без падений и ложных рёбер |
| S4 | `d43be69` | GUI: тип snowflake в диалоге подключения, дефолт-порт 443 (порт-дефолты переключаются на stock-значениях, кастомный порт не затирается), подсказка browser-auth на поле пароля, сохранение без пароля |

## Ключевая находка: case-чувствительность word-scan

Индекс имён парсера строился из имён объектов «как есть», а токены
нормализованного потока — lowercase: `from orders` не находил `ORDERS`.
PG/GP (всё lowercase) не показывали проблему; Snowflake хранит unquoted
идентификаторы UPPERCASE — рёбра молча не строились бы. Фикс: keys
`_build_names_index`/`_build_overload_index` lowercased, qualified-фолбэки
lowercased, `find_calls` — `re.IGNORECASE`. Регресс Phase 8 зелёный
(lowercase-мир идентичен).

## Решения и ограничения MVP

- **Autoincrement**: Snowflake `<seq>.NEXTVAL` в COLUMN_DEFAULT → колонка
  без default (PG-ветка `GENERATED AS IDENTITY` неприменима, Snowflake
  использует AUTOINCREMENT) — фикс на этапе деплой-фазы.
- **CHECK-констрейнты** пропускаются (hybrid-tables only); **indexes: []**
  (стандартные таблицы Snowflake без индексов).
- **Comment-DDL**: шаблоны рендерят PG-диалект `COMMENT ON ...` — для
  деплоя в Snowflake потребуется правка (вероятно `ALTER ... SET COMMENT`);
  на граф/парсинг не влияет.
- **SHOW/IS SQL «вслепую»**: перечень команд и колонок SHOW написан по
  документации; live-смоук подтвердит имена колонок (запросы адаптированы
  под dict-строки с `.get`-фолбэками).
- ACCOUNT_USAGE не используется (латency/права) — только SHOW + IS.

## Проверки

- `uv run pytest` — **1382 passed, 45 deselected** (+39 к фазе: 26 S1,
  8 S2-структура+RE-e2e, 2 S3-пре, 5 S4-GUI); ruff чисто.
- Unit-тесты без драйвера: фейковый модуль в sys.modules (S1), фикстурные
  SHOW/GET_DDL/IS-строки (S2/S3), offscreen QApplication (S4).
- **Live-смоук не гонялся** — нужен аккаунт Snowflake:
  1. `uv sync --extra snowflake`;
  2. connection-yaml: `type: snowflake`, `host: <account>.snowflakecomputing.com`,
     `options: {authenticator: externalbrowser, warehouse: ..., role: ...}`;
  3. смоук: браузерная регистрация → повторный connect без браузера (кэш
     токена) → `reverse-engineer` → `graph build`/`validate` → GUI-подключение.

## OUT of scope (следующие шаги)

- **Деплой в Snowflake** — отдельная фаза (validate/apply/reset; временные
  БД в Snowflake — клоны схем).
- Key-pair аутентификация — BACKLOG P3 (запись создана).
- External/dynamic tables, streams, tasks — по запросу.
- MCP-инструменты (`run_query`/`explain`) — сурфейс готов, приделать после
  live-проверок.

---

*Файл: `harness_docs/harness_tasks/2026-11-03/20261103_002_snowflake_connect_re_result.md`*

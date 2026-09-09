# Phase 15.9 (plan): deploy validate — preflight CREATEDB не учитывает суперюзера

> Контекст:
> - `_docs_/_tasks_/phase_13/Phase_13.md`, `_checkpoints_/20260908_001_checkpoint.md`
> - `LESSONS_LEARNED.md` §19 (whitelist в CREATE DATABASE) — соседняя тема прав/DDL
> - Feedback: первый тестовый деплой на Greenplum (LM.HRDO-2.DEV, 2026-09-08)

**Дата:** 2026-09-08
**Статус:** план (диагноз подтверждён живым запросом к серверу пользователя)

---

## 1. Симптом

```bash
db-pm deploy validate --dir .../greenplum/current_dev/cis_zup \
    --connection-file connections/IVSD00258..._U_gpadmin.yaml --keep-db --continue-on-error
# ОШИБКА: Нет прав CREATEDB: У пользователя 'gpadmin' нет права CREATEDB.
```

При этом пользователь **руками успешно создал базу** под тем же `gpadmin`.

## 2. Диагноз (подтверждён)

Живой запрос через адаптер проекта к IVSD00258.reksoft.com:5433/demo:

```
role attributes: [('gpadmin', rolsuper=True, rolcreatedb=False)]
check_can_create_db() -> False
```

- Preflight-проверка — `infrastructure/database/postgres/queries.py::GET_CREATEDB_CHECK`:
  `SELECT rolcreatedb FROM pg_roles WHERE rolname = current_user` — смотрит
  **только** `rolcreatedb`.
- В PostgreSQL/Greenplum `rolsuper` и `rolcreatedb` — независимые атрибуты:
  `CREATE ROLE ... SUPERUSER` не ставит CREATEDB. При этом **суперюзер
  обходит проверку привилегий на CREATE DATABASE** — поэтому ручное создание
  прошло, а preflight отказал.
- Не GP-специфика: суперюзер без CREATEDB на обычном PG попал бы туда же.
  Гипотеза пользователя про «owner от PG, а не GP» — направление верное
  (проверка не учитывает модель ролей), точная причина — игнор `rolsuper`.

## 3. Решение

`GET_CREATEDB_CHECK`:

```sql
SELECT rolsuper OR rolcreatedb
  FROM pg_roles
 WHERE rolname = current_user
```

- Один метод `PGDatabaseAdapter.check_can_create_db` — закрывает `deploy
  validate` и `deploy apply` (репетиция) одновременно.
- Текст ошибки в `deploy_service.py` остаётся корректным: срабатывает только
  когда оба флага false.

## 4. Проверки

- Регрессионный тест в `tests/unit/test_queries.py`: контракт запроса —
  `rolsuper OR rolcreatedb` (guards against молчаливый откат к `rolcreatedb`).
- Прогон unit-тестов + ruff (обход Device Guard, LESSONS §68).
- Ручная проверка пользователем: повторный `deploy validate` на том же
  подключении gpadmin должен пройти preflight.

## 5. NOT in scope

- Изменение CREATE DATABASE (owner/template fallback) — не требуется:
  ручное создание доказало, что DDL работает.
- Кэширование/обход preflight-флагами.

# Phase 15.9 (result): deploy validate — preflight CREATEDB учитывает суперюзера

> Контекст:
> - `_docs_/_tasks_/phase_15/Phase_15.9_plan.md` — план с подтверждённым диагнозом
> - `LESSONS_LEARNED.md` §69 — урок фазы

**Дата:** 2026-09-08
**Статус:** реализовано, проверено на живом GP-сервере пользователя

---

## Что сделано

Preflight-проверка прав `deploy validate` / `deploy apply` (репетиция) больше
не отклоняет суперюзеров без явного атрибута CREATEDB.

| Файл | Изменение |
|---|---|
| `src/db_project_manager/infrastructure/database/postgres/queries.py` | `GET_CREATEDB_CHECK`: `SELECT rolsuper OR rolcreatedb FROM pg_roles WHERE rolname = current_user` |
| `tests/unit/test_queries.py` | `test_createdb_check_accounts_for_superuser` — контракт «rolsuper OR rolcreatedb» (guards против отката к rolcreatedb-only) |
| `LESSONS_LEARNED.md` | §69 — привилегия ≠ атрибут роли; суперюзер обходит privilege checks |

## Проверки

```bash
# Живой сервер пользователя (IVSD00258.reksoft.com:5433/demo, gpadmin):
# до фикса:  check_can_create_db() -> False  (rolsuper=True, rolcreatedb=False)
# после:     check_can_create_db() -> True

PYTHONPATH="src;.venv/Lib/site-packages" ~/AppData/Roaming/uv/python/cpython-3.13.*/python.exe \
    -m pytest tests/unit/ -q -p no:randomly
# 966 passed (965 + 1 новый)

.venv/Scripts/ruff.exe check src/ tests/
# All checks passed!
```

## Известные ограничения

- Текст ошибки `deploy_service.py` («нет права CREATEDB») теперь срабатывает
  только когда оба флага false — формулировка осталась корректной.
- Поведение `CREATE DATABASE` не менялось (ручное создание пользователем уже
  доказало работоспособность DDL-пути).

## Следующий шаг пользователя

Повторить на том же подключении:

```bash
db-pm deploy validate --dir .../greenplum/current_dev/cis_zup \
    --connection-file connections/IVSD00258..._U_gpadmin.yaml \
    --keep-db --continue-on-error
```

Preflight должен пройти; дальнейшие сообщения (если будут) — уже про сам
деплой корпуса, не про права.

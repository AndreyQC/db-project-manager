# Явное имя временной БД в deploy validate — result

> Контекст:
> - `20261009_001_deploy_validate_db_name_plan.md` — план
> - `_docs_/_checkpoints_/20261007_001_checkpoint.md`

**Дата:** 2026-10-09
**Статус:** ЗАКРЫТА

## Что сделано

Добавлен параметр `--db-name` в `deploy validate`:

| Файл | Изменение |
|------|-----------|
| `src/db_project_manager/application/deploy_service.py` | Параметр `db_name: str \| None = None`; валидация через `_validate_db_name()`; логика: если передан — используется, иначе генерируется |
| `src/db_project_manager/presentation/cli/main.py` | Флаг `--db-name`, передача в `service.run()`, обработка `ValueError` (exit code 2) |

## Проверки

| Проверка | Результат |
|----------|-----------|
| `uv run ruff check .` | ✓ чистый |
| `uv run pytest tests/unit/` | ✓ 1271 passed |
| `--help` показывает `--db-name` | ✓ |

## Known limitations

- Пользователь обязан сам удалить базу после завершения сценария
- Нет отдельной команды `deploy cleanup-temp-db` (можно использовать `psql -c "DROP DATABASE"`)

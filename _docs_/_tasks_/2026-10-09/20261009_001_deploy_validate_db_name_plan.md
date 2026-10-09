# План: явное имя временной БД в deploy validate — plan

> Контекст:
> - `_docs_/_checkpoints_/20261007_001_checkpoint.md` — текущее состояние
> - `LESSONS_LEARNED.md` — уроки по архитектуре

**Дата:** 2026-10-09
**Статус:** РЕАЛИЗОВАНО

## Цель

Позволить пользователю передавать явное имя временной БД в `deploy validate` вместо автогенерации `prefix_timestamp`. Это нужно для CI/CD сценариев: после validate запустить сид-скрипты и тесты, затем удалить базу вручную.

## Решение

Добавить параметр `--db-name` в CLI и `db_name` в `DeployValidateService.run()`:

- Если `--db-name` передан — используется как есть
- Если не передан — генерируется по текущей логике `prefix_timestamp`

### Изменения

1. **`application/deploy_service.py`:**
   - Новый параметр `db_name: str | None = None` в `run()`
   - Если `db_name is None` — генерация по старой логике
   - Валидация через `_validate_db_name()` (имя должно быть `^[A-Za-z_][A-Za-z0-9_]*$`)

2. **`presentation/cli/main.py`:**
   - Новый флаг `--db-name` в команде `deploy validate`
   - Передача `db_name` в `service.run()`
   - Обработка `ValueError` при некорректном имени (exit code 2)

## Проверки

- [ ] `uv run ruff check .` — чистый
- [ ] `uv run pytest tests/unit/` — все проходят
- [ ] `--help` показывает новый флаг

## Использование

```bash
# CI/CD сценарий:
DB_NAME="test_$(date +%Y%m%d_%H%M%S)"
db-pm deploy validate \
    --dir ./mydb \
    --connection-file server.yaml \
    --db-name "$DB_NAME" \
    --keep-db

# Запуск тестов / сид-скриптов на "$DB_NAME" ...

# Удаление:
psql -h localhost -U postgres -c "DROP DATABASE \"$DB_NAME\";"
```

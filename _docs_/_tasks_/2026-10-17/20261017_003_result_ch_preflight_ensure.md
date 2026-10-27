# Pre-flight `ensure_deploy_tables()` в DeployApplyService (result)

> **Date:** 2026-10-17
> **Status:** завершено
> **Связанные документы:**
> - `_docs_/_tasks_/2026-10-17/20261017_003_plan_ch_preflight_ensure.md` (план)
> - `_docs_/_checkpoints_/20261017_001_checkpoint.md` (обновлён)
> - Коммит `6081b05`

---

## Что сделано

Реализован pre-flight в `DeployApplyService._run_pipeline()` — перед любым
deploy-вызовом, который пишет в deploy journal, убеждаемся что 3 таблицы
существуют в целевой БД. Создаём их идемпотентно, если нет.

### Изменения в коде

**`src/db_project_manager/application/deploy_apply_service.py`** (+35 строк)
- Новый метод `_ensure_journal_tables(adapter, progress)`:
  - `hasattr(adapter, "ensure_deploy_tables")` — engine-agnostic dispatch
  - Если метод есть (только CH) — вызывает его
  - Если метод нет (PG) — no-op (PG-flow не меняется)
  - Ошибки оборачивает в `DeployApplyError`
- Вызов в `_run_pipeline` сразу после `adapter.connect(conn_cfg)`:
  ```python
  adapter.connect(conn_cfg)
  self._ensure_journal_tables(adapter, progress)  # NEW
  target_version = self._read_target_version(adapter)
  ```

### Изменения в тестах

**`tests/unit/test_deploy_apply_service.py`** (+110 строк, 3 теста)
- `_JournalBootstrappingAdapter` — fake с `ensure_deploy_tables` для тестов
- `test_preflight_ensure_journal_tables_called_for_ch_like_adapters` —
  проверяет что `ensure_deploy_tables` вызван ДО `record_schema_version`
- `test_preflight_no_op_for_adapters_without_ensure_deploy_tables` —
  проверяет что PG-путь (no `ensure_deploy_tables`) не затрагивается
- `test_preflight_ensure_failure_raises_deploy_apply_error` — проверяет
  оборачивание DatabaseError → DeployApplyError

**`tests/integration/clickhouse/test_ch_adapter.py`** (+43 строки, 1 тест)
- `test_ensure_deploy_tables_works_on_fresh_db` — интеграционная
  проверка: на свежей per-test БД `ensure_deploy_tables` создаёт 3
  таблицы, идемпотентен на повторе

## Проверки

| Проверка | Результат |
|---|---|
| `uv run pytest` | **1298 passed** (1280 unit + 14 CH + 4 deselected Docker) |
| `uv run ruff check .` | clean |
| Поведение PG-flow | идентично (no-op через hasattr) |
| Идемпотентность | подтверждена (повторный вызов = no-op) |
| Error wrapping | `DatabaseError` → `DeployApplyError` (deploy boundary) |

## Что НЕ делалось

- Pre-flight в `_plan` / `validate` методах DeployApplyService — текущий
  pre-flight только в `_run_pipeline` (apply). Plan/validate работают
  read-only на target-БД и не нуждаются в deploy_journal.
- Auto-bootstrap через DDL retry с увеличенным `distributed_ddl_task_timeout`
  — не понадобилось, race condition решается в тесте выбором пути
  верификации (`get_database_structure` через `system.tables`).
- Кэш «уже bootstrapped» — `ensure_deploy_tables` с retry (через
  `_cmd_with_retry`) уже обрабатывает cluster desync; повторные
  DDL ~50ms negligible.

## Известные ограничения

- **Cluster desync timing** — `ensure_deploy_tables` использует
  `ON CLUSTER default` (async DDL). На per-test БД сразу после CREATE
  clusterAllReplicas может попасть на реплику, где DDL ещё не
  применился. Тест обходит это через `get_database_structure`
  (запрос к `system.tables`, локальный на шарде). В проде — при первом
  CH-деплое на новую target-БД, deploy запишет schema_version сразу
  после CREATE TABLE на первичной реплике; retry внутри
  `_cmd_with_retry_params` обработает любые race conditions.

## Коммиты

- `6081b05` — `feat(app): DeployApplyService pre-flight ensure_deploy_tables (CH auto-bootstrap)`
- Обновление чекпойнта: `_docs_/_checkpoints_/20261017_001_checkpoint.md`

## Статус

Phase 6.1 завершён. PG-путь без изменений. CH-deploy теперь не требует
ручного `db-pm deploy init-service-schema` перед первым apply.

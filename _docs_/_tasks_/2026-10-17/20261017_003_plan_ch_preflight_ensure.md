# Pre-flight `ensure_deploy_tables()` в DeployApplyService (plan)

> **Date:** 2026-10-17
> **Status:** в работе (план)
> **Связанные документы:**
> - `_docs_/_checkpoints_/20261017_001_checkpoint.md` (Phase 5.6 — per-target deploy journal)
> - `_docs_/_tasks_/2026-10-17/20261017_002_analysis_pg_gp_split.md` (отдельная задача, не CH)
> - `_docs_/_tasks_/2026-10-16/20261016_001_clickhouse_support_plan.md` (общий план CH)
> - `src/db_project_manager/application/deploy_apply_service.py` — точка вставки

---

## Цель

Сейчас для CH-деплоя пользователь должен **вручную** вызвать
`db-pm deploy init-service-schema` перед первым `db-pm deploy apply`
(иначе `record_schema_version` упадёт с «table not found»). Это тот же
ручной шаг, который требуется в PG при ручном bootstrap.

**Цель:** сделать bootstrap автоматическим на стороне `DeployApplyService` —
перед любым deploy-вызовом, который пишет в deploy journal, убедиться что
3 таблицы (`deploy_schema_version` / `deploy_script_history` /
`deploy_script_audit_log` для CH, `__deploy.{table}` для PG) существуют в
целевой БД. Создать их, если нет.

PG-путь **не** автоматизируем — там уже устоявшийся ручной flow через
`init-service-schema`. Цель — только закрыть разрыв в CH UX.

## Решение

Добавить **pre-flight** в `DeployApplyService._run_pipeline()` сразу после
`adapter.connect(conn_cfg)` (строка 367) и перед
`_read_target_version(adapter)` (строка 368):

```python
adapter.connect(conn_cfg)
self._ensure_journal_tables(adapter)             # NEW
target_version = self._read_target_version(adapter)
```

### Новый метод `_ensure_journal_tables`

```python
def _ensure_journal_tables(self, adapter: DatabaseAdapter) -> None:
    """Idempotent pre-flight: ensure deploy journal tables exist.

    For CH (per-target tables in service_schema/target_db): calls
    ``adapter.ensure_deploy_tables(self._service_schema)`` which issues
    3 ``CREATE TABLE IF NOT EXISTS ... ON CLUSTER default`` statements.

    For PG: no-op. The PG ``__deploy`` schema is bootstrapped explicitly
    via ``ServiceSchemaInitializer`` (and the deploy apply path is wired
    to require it to exist; missing schema surfaces as a clear error).

    We dispatch via ``hasattr(adapter, "ensure_deploy_tables")`` so this
    stays engine-agnostic — new adapters opt in by implementing the
    method.
    """
    if not hasattr(adapter, "ensure_deploy_tables"):
        return
    self._emit(progress, "Bootstrap deploy journal…", 0, 0)
    try:
        adapter.ensure_deploy_tables(self._service_schema)
    except DatabaseError as e:
        raise DeployApplyError(
            f"Не удалось создать таблицы deploy-journal в "
            f"{self._service_schema}: {e}"
        ) from e
```

### Почему именно так

- **`hasattr` dispatch** — engine-agnostic; новые адаптеры (Snowflake, MSSQL)
  подключаются добавлением `ensure_deploy_tables` без правки service-слоя.
- **Только для CH** — для PG метод отсутствует, no-op. PG-flow не трогаем
  (минимальный blast radius).
- **Сразу после `connect`** — каждый deploy гарантированно стартует с
  валидным состоянием. `_read_target_version` для свежей БД возвращает
  `None` (как и сейчас), но если раньше упал бы `record_schema_version`
  в конце — теперь упадёт **раньше и с понятным сообщением**.
- **Idempotent** — `ensure_deploy_tables` уже идемпотентен (CREATE
  TABLE IF NOT EXISTS); повторные вызовы безопасны.

## Точки вставки (в `DeployApplyService._run_pipeline`)

**`src/db_project_manager/application/deploy_apply_service.py`**:
- Строка 367-368 — между `adapter.connect(conn_cfg)` и
  `target_version = self._read_target_version(adapter)`.
- Импорты: `DatabaseError` уже импортирован (используется в строке 498).

## Тесты

### Новые

1. **`tests/unit/test_deploy_apply_preflight.py`** — мокаем адаптер с
   `ensure_deploy_tables`, проверяем что метод вызван перед
   `_read_target_version`. Мокаем без `ensure_deploy_tables` — проверяем
   no-op.

2. **`tests/integration/clickhouse/test_ch_adapter.py`** — расширяем
   `test_deploy_journal_roundtrip` или добавляем
   `test_deploy_apply_creates_journal_on_first_run`: создаём пустую БД
   (без `ensure_deploy_tables`), запускаем `DeployApplyService` (или
   прямой адаптер), проверяем что 3 таблицы созданы автоматически.

### Что не меняем

- PG e2e (`test_deploy_phase10_e2e.py`) — PG-путь не затрагивается.
- Существующие CH-тесты — `ensure_deploy_tables` теперь no-op (т.к.
  таблицы уже созданы в `setUp`), поведение идентично.

## Риски

1. **Cluster desync** — `ensure_deploy_tables` уже содержит retry на
   `UNKNOWN_DATABASE` (через `_cmd_with_retry`). Mitigated.
2. **Лишний DDL на каждый deploy** — 3 `CREATE TABLE IF NOT EXISTS` (~50ms
   суммарно) на каждый apply. Negligible. Можно опционально закэшировать
   «уже bootstrapped» флаг в адаптере, но не в Phase 6.1.
3. **Изменение семантики `_read_target_version`** — раньше для свежей CH-БД
   возвращался `None` через warning swallow (строка 622-624), потом
   deploy_apply_service интерпретировал как «first deploy». Сейчас —
   `ensure_deploy_tables` сначала создаёт таблицы, потом `_read_target_version`
   вернёт `None` (т.к. таблицы пустые). Поведение идентично, но **ошибка
   table-not-found больше не маскируется** в `record_schema_version` —
   теперь она возникнет раньше и с понятным сообщением.
4. **PG regression** — у PG нет `ensure_deploy_tables`, `hasattr` вернёт
   False, no-op. PG-flow не затрагивается. Подтверждается
   `test_deploy_phase10_e2e.py` (если Docker доступен; иначе —
   ручной sanity check).

## Чекпойнт

После реализации обновить `_docs_/_checkpoints_/20261017_001_checkpoint.md`:
- Добавить раздел «Phase 6.1 — Pre-flight ensure_deploy_tables» с деталями.
- Пометить Phase 6 #1 как done.
- Обновить таблицу сводки фаз.

## Готово

Когда реализация завершена:
1. Обновить `_result.md` с фактическими деталями.
2. Закоммитить код (отдельный коммит от плана).
3. Обновить чекпойнт.

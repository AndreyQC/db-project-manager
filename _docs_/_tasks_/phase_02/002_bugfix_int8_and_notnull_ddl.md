# Bugfix: DDL-рендеринг — int8(64,0) и NOT NULL constraint

> Контекст:
> - `_tasks_\phase_00\003_roadmap_migration.md` — roadmap
> - `_tasks_\phase_02\Phase_2_vision_final.md` — Phase 2 decisions
> - `src/db_project_manager/infrastructure/sql/sql_generator.py` — источник Bug A
> - `src/db_project_manager/infrastructure/database/postgres/adapter.py` — источник Bug B
> - `src/db_project_manager/infrastructure/templates/table.sql.j2` — шаблон таблицы
> - Дата: 2026-07-19

---

## 1. Предыстория / источник

При **Deploy Validate** через UI для dagster DB (`D:\tune-db-manager\local-pg-db-dagster\dagster`) возникает ошибка валидации:

```sql
CREATE TABLE public.asset_check_executions (
    id int8(64, 0) NOT NULL DEFAULT nextval('asset_check_executions_id_seq'::regclass),
                                ^—
```

Аналогичные дефекты во всех таблицах с `bigint`-колонками и NOT NULL PK.

**Стейкхолдер:** разработчик, запускающий `db-pm deploy validate` на реальной DB.

---

## 2. Bug A — `int8(64, 0)` вместо `bigint`

### 2.1 Описание

`type_mod()` в `sql_generator.py:30` без разбора добавляет `(numeric_precision, numeric_scale)` к любому типу колонки:

```python
def type_mod(col):
    np, ns = col.get("numeric_precision"), col.get("numeric_scale")
    if np is not None and ns is not None:
        return f"({np}, {ns})"   # → для bigint выдаёт "(64, 0)"
```

PostgreSQL хранит в `information_schema` для `bigint` значения `numeric_precision=64, numeric_scale=0`. Синтаксис `int8(64, 0)` **невалиден** — PostgreSQL поддерживает `(precision, scale)` только для `numeric`/`decimal`. Для `int8` правильный вывод — просто `bigint` (или `int8` без модификаторов).

### 2.2 Затронутые типы

Колонки dagster DB с дефектом:
- `asset_check_executions(id)` — PK `bigint` → `int8(64, 0)`
- `asset_check_executions(evaluation_event_storage_id)` — `bigint` → `int8(64, 0)`
- `asset_check_executions(materialization_event_storage_id)` — `bigint` → `int8(64, 0)`
- Все таблицы dagster: `runs(id)`, `jobs(id)`, `event_logs(id)` и т.д.

### 2.3 Решение

В `type_mod()` — **не** добавлять `(precision, scale)` для типов, где эти атрибуты синтаксически не имеют значения:

```python
# Типы, для которых (precision, scale) из information_schema не применимы в DDL
_TYPES_WITHOUT_NUMERIC_MOD = frozenset({
    # Целые числа
    'int2', 'int4', 'int8', 'smallint', 'integer', 'bigint',
    'smallserial', 'serial', 'bigserial',
    # Вещественные без точности/масштаба
    'float4', 'float8', 'real', 'double precision',
    # Прочие
    'bool', 'boolean', 'bytea', 'date', 'time', 'timetz',
    'timestamp', 'timestamptz', 'interval',
    'money', 'oid', 'uuid', 'xml', 'json', 'jsonb',
    'text', 'bpchar', 'char', 'name', 'varchar',
    # array и т.п.
})
```

**Критерий приёмки:** unit-тесты на `type_mod` с `bigint`, `numeric(10,2)`, `varchar(255)`, `float8`, `int4` — убедиться что `(precision, scale)` добавляются только к `numeric`/`decimal`.

---

## 3. Bug B — NOT NULL как named CONSTRAINT

### 3.1 Описание

При reverse-engineer из live DB в `adapter.py:210-212` **все** constraints попадают в общий список:

```python
constraints = self._group_constraints(
    self._exec(q.GET_CONSTRAINTS, {"table_name": name, "schema": schema})
)
```

В `information_schema` **NOT NULL** записывается как `constraint_type = 'CHECK'`. PostgreSQL `pg_get_constraintdef(c.oid)` для него возвращает `NOT NULL <colname>` (например `NOT NULL id`).

Шаблон `table.sql.j2:7` рендерит:
```sql
CONSTRAINT {{ c.name }} {{ c.definition }}
```

Результат — **невалидный** DDL:
```sql
CONSTRAINT asset_check_executions_id_not_null NOT NULL id
```

Правильный синтаксис: `id bigint NOT NULL` — `NOT NULL` это column modifier, а не constraint.

### 3.2 Затронутые колонки

Во всех dagster-таблицах с NOT NULL PK/FK:
```sql
CONSTRAINT asset_check_executions_id_not_null NOT NULL id      -- невалиден
CONSTRAINT runs_id_not_null NOT NULL id                         -- невалиден
CONSTRAINT event_logs_event_not_null NOT NULL event             -- невалиден
```

### 3.3 Решение

В `adapter.py` метод `_build_table()` — **исключить** NOT NULL CHECK-constraints из списка `constraints`, т.к. `nullable` уже определён через `col[2] == "YES"`:

```python
# Убрать NOT NULL check-constraints — они не имеют DDL-следов
# (nullable уже выставлен через col[2] == "YES")
constraints = [
    c for c in constraints
    if not (c["type"] == "CHECK" and c["definition"].startswith("NOT NULL"))
]
```

**Альтернатива (не выбрана):** править шаблон — определять, начинается ли `definition` с `NOT NULL`, и рендерить иначе. Не выбрана, т.к. это нарушает семантику — NOT NULL в constraint-списке это артефакт `information_schema`, а не валидный constraint.

---

## 4. Bug C — Зарезервированные слова как имена колонок без кавычек

### 4.1 Описание

Все идентификаторы в SQL-шаблонах выводились **без кавычек**. Когда колонка или таблица называется зарезервированным словом PostgreSQL (`limit`, `user`, `order`, `group` и т.д.), PostgreSQL отвергает DDL:

```sql
limit int4 NOT NULL   -- ← syntax error at "limit"
```

Источник — шаблоны `.sql.j2` рендерят `{{ col.name }}` напрямую.

### 4.2 Решение

Добавить helper `_qi()` (quote identifier) в `sql_generator.py` и применить ко **всем** идентификаторам в шаблонах:

```python
def qi(name: str) -> str:
    """Double-quote a SQL identifier, doubling embedded quotes."""
    return '"' + str(name).replace('"', '""') + '"'
```

Подход **всегда квотировать** — безопасен для всех СУБД (стандарт SQL), нет накладных расходов, не зависит от версии PG.

Добавлены:
- `postgres/keywords.yaml` — PG 18 reserved words (источник: официальная документация)
- `postgres/keywords.py` — загрузчик, `get_reserved()` (для будущего явного использования; текущий `_qi()` не зависит от этого набора)
- `adapter.get_database_structure()` → `reserved_keywords` в structure dict (для будущих проверок)

### 4.3 Затронутые шаблоны

Все 9 шаблонов обновлены: `table`, `view`, `materialized_view`, `sequence`, `schema`, `function`, `procedure`, `trigger`, `extension`.

---

## 5. План работ

| Шаг | Действие | Файл |
|-----|----------|------|
| S17a | Исправить `type_mod()` — пропускать `(precision, scale)` для типов без модификаторов | `sql_generator.py` |
| S17b | Исправить `_build_table()` — удалить NOT NULL CHECK-constraints из списка | `adapter.py` |
| S17c | Добавить unit-тесты `test_type_mod` | `tests/` |
| S17d | Добавить `_qi()` helper + применить во всех шаблонах | `sql_generator.py` + 9 `.j2` |
| S17e | Добавить `postgres/keywords.yaml` + `keywords.py` | `postgres/` |

---

## 6. Метрики приёмки

1. `uv run pytest tests/unit/` — 160 тестов зелёные
2. `uv run ruff check src/` — чистый
3. Deploy validate dagster DB — **0 errors** (все 43 таблицы созданы)
4. Повторный reverse-engineer dagster DB → deploy validate — детерминирован

---

## 7. Файлы для изменения

```
src/db_project_manager/infrastructure/sql/sql_generator.py       # type_mod, _qi
src/db_project_manager/infrastructure/database/postgres/adapter.py    # reserved_keywords in structure
src/db_project_manager/infrastructure/database/postgres/keywords.py   # get_reserved()
src/db_project_manager/infrastructure/database/postgres/keywords.yaml # PG 18 reserved words
src/db_project_manager/infrastructure/templates/*.sql.j2            # _qi() applied
tests/unit/test_sql_generator.py                                # updated assertions + new tests
```

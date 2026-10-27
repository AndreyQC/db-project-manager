# CH profiler (Phase 6.2 — plan)

> **Date:** 2026-10-17
> **Status:** в работе (план)
> **Связанные документы:**
> - `_docs_/_checkpoints_/20261017_001_checkpoint.md` (Phase 6.1 завершён)
> - `_docs_/_tasks_/2026-10-16/20261016_001_clickhouse_support_plan.md` (исходный план CH)
> - `_docs_/_tasks_/2026-10-17/20261017_002_analysis_pg_gp_split.md` (отдельная задача, не CH)
> - `src/db_project_manager/infrastructure/profiling/{base,postgres,greenplum}.py` (образцы)
> - `src/db_project_manager/infrastructure/profiling/__init__.py` — `get_profiler()`

---

## Цель

`profile_tables` MCP-инструмент сейчас падает на CH-подключениях:
`infrastructure/profiling/__init__.py::get_profiler("clickhouse")` бросает
`ValueError`, что превращается в `ProfilingError` на уровне сервиса. Это
единственный неработающий MCP-tool для CH (Phase 6 backlog #2).

**Цель:** реализовать `CHProfiler(ProfilingSQLGenerator)` по образцу
`PostgresProfiler` / `GreenplumProfiler`, и зарегистрировать его в
`get_profiler("clickhouse")`. После этого `profile_tables` будет
возвращать результат для CH-таблиц (как сейчас делает для PG/GP).

## CH-специфика (отличия от PG/GP)

| Концепция | PG/GP | CH |
|---|---|---|
| Каталог колонок | `information_schema.columns` | `system.columns` |
| Каталог таблиц | `pg_class` + `pg_stat_user_tables` | `system.tables` + `system.parts` |
| Сэмплирование | `TABLESAMPLE SYSTEM (p)` | `SAMPLE k` (1/k строк) |
| Random sample | `WHERE random() < p` | `WHERE cityHash64(...) % k = 0` или `rand() % k = 0` |
| `percentile_cont(...)` | есть | нет → `quantile(f)(x)` |
| `stddev_samp` | есть | есть |
| `length(x::text)` | есть | `length(x)` (без приведения) |
| `min(x) / max(x)` для строк | есть | есть |
| `count(DISTINCT x)` | есть | есть |

### SAMPLE k
- `SAMPLE k` — детерминированный сэмпл (1/k строк) — нативно для CH
- Только для MergeTree family; для View/Dictionary/Null SAMPLE не работает
  → fallback на `rand() < p` (через `sample_base_expr_random`)
- Семантика в нашем коде: `fraction=0.1` → `SAMPLE 10` (1 строка из 10)

### percentiles
- `percentile_cont(0.5) WITHIN GROUP (ORDER BY x)` — PG
- `quantile(0.5)(x)` — CH (aggregatingMergeTree-style function, не ordered-set)
- Сравнение: результат тот же (медиана/квантили), но синтаксис другой

## Решение

Создать `infrastructure/profiling/clickhouse.py` с классом `CHProfiler`,
реализующим 4 абстрактных метода `ProfilingSQLGenerator`:
- `columns_sql(schema, table)` — `system.columns` через `clusterAllReplicas`
- `table_meta_sql(schema, table)` — `system.tables` + `system.parts`
- `parse_meta(row)` — `TableMeta(reltuples=total_rows, size_bytes=total_bytes, ...)`
- `base_expr(schema, table, fraction)` — `SAMPLE k` если fraction, иначе `db.table`
- `sample_base_expr_random(schema, table, fraction)` — `WHERE rand() < p` fallback

И подкласс `CHProfilerOverrides` в `base.py` для переопределения
`main_sql` и `histogram_sql` (CH-специфичные функции: `quantile` вместо
`percentile_cont`, `length(x)` без `::text`).

Зарегистрировать в `get_profiler()` (1 строка).

## Файлы

### Создать
- `src/db_project_manager/infrastructure/profiling/clickhouse.py` (~150 строк)
- `tests/integration/clickhouse/test_ch_profiler.py` (новые интеграционные тесты)

### Изменить
- `src/db_project_manager/infrastructure/profiling/__init__.py` (+1 строка)
- `src/db_project_manager/infrastructure/profiling/base.py` (опционально — `CHProfilerOverrides` если main_sql/histogram_sql нужно переопределить)

## Точки вставки

### `infrastructure/profiling/clickhouse.py` (новый)
```python
class CHProfiler(ProfilingSQLGenerator):
    """CH 25.x SQL-profiling generator."""

    def columns_sql(self, schema, table):
        return (
            f"SELECT name AS column_name, type AS data_type, "
            f"NOT startsWith(type, 'Nullable(') AS nullable "
            f"FROM clusterAllReplicas('default', system.columns) "
            f"WHERE database = {quote_literal(schema)} AND table = {quote_literal(table)} "
            f"ORDER BY position"
        )

    def table_meta_sql(self, schema, table):
        return (
            f"SELECT t.total_rows AS reltuples, "
            f"t.total_bytes AS size_bytes, "
            f"p.parts_active, p.parts_total "
            f"FROM clusterAllReplicas('default', system.tables) t "
            f"LEFT JOIN ("
            f"  SELECT database, table, "
            f"         countIf(active=1) AS parts_active, count() AS parts_total "
            f"  FROM clusterAllReplicas('default', system.parts) "
            f"  GROUP BY database, table"
            f") p ON p.database = t.database AND p.table = t.name "
            f"WHERE t.database = {quote_literal(schema)} AND t.name = {quote_literal(table)}"
        )

    def parse_meta(self, row):
        return TableMeta(
            reltuples=_as_int_or_none(row.get("reltuples")),
            size_bytes=_as_int_or_none(row.get("size_bytes")),
            stats_present=row.get("reltuples") is not None,
        )

    def base_expr(self, schema, table, fraction):
        qualified = f"{quote_ident(schema)}.{quote_ident(table)}"
        if fraction is None:
            return qualified
        # SAMPLE k работает только для MergeTree; для View/Dict fallback в random
        k = max(2, int(round(1.0 / fraction)))
        return f"{qualified} SAMPLE {k}"

    def sample_base_expr_random(self, schema, table, fraction):
        return f"{self._qualified(schema, table)} WHERE rand() < {fraction}"
```

### `infrastructure/profiling/__init__.py` (+1)
```python
from db_project_manager.infrastructure.profiling.clickhouse import CHProfiler

# в get_profiler:
if normalized == "clickhouse":
    return CHProfiler()
```

### Опционально: `infrastructure/profiling/base.py` (для переопределения main_sql/histogram_sql)

Базовый `main_sql` использует `percentile_cont(...)` (PG-функция). Для CH
нужен `quantile(f)(x)`. Если не переопределять — профайлер упадёт на
профиле числовых колонок. **Решение:** создать mixin-класс
`QuantileAggregator` (или просто override в `CHProfiler`):
```python
def main_sql(self, columns, base):
    # копия base.main_sql + замена percentile_cont на quantile
    ...
```

Это большой кусок копипасты (~35 строк). Альтернатива — вынести общую
логику в `ProfilingSQLGenerator` и parameterize через `percentile_func(self)`
property (PG → `percentile_cont({f}) WITHIN GROUP (ORDER BY {q})`, CH →
`quantile({f})({q})`).

**Рекомендация:** parameterize `percentile_func` (минимальный диф, обе
реализации остаются чистыми). Меняем `base.py`:
```python
class ProfilingSQLGenerator(ABC):
    @property
    def percentile_expr(self, fraction: float, column_ref: str) -> str:
        """Engine-specific percentile aggregation expression."""
        raise NotImplementedError  # subclasses override

    def main_sql(self, columns, base):
        ...
        for frac in PERCENTILE_FRACTIONS:
            parts.append(
                f'{self.percentile_expr(frac, q)} AS "{a}{key}"'
            )
```
- PG: `return f"percentile_cont({frac}) WITHIN GROUP (ORDER BY {q})"`
- CH: `return f"quantile({frac})({q})"`

Это ~10 строк изменений в base.py + override в `PostgresProfiler` и `GreenplumProfiler`
(которые могут наследовать от `PostgresProfiler`-стиля). А `CHProfiler.main_sql`
наследует без переопределения.

## Тесты

### Новые

1. **`tests/integration/clickhouse/test_ch_profiler.py`** — 4-6 тестов:
   - `test_ch_profiler_columns_sql` — проверяет что запрос возвращает колонки для существующей таблицы
   - `test_ch_profiler_table_meta_sql` — возвращает reltuples, size_bytes
   - `test_ch_profiler_base_expr_with_sample` — генерирует `SAMPLE k` для fraction
   - `test_ch_profiler_sample_base_expr_random` — генерирует `WHERE rand() < p`
   - `test_ch_profiler_base_expr_no_sample` — возвращает полное имя таблицы
   - `test_ch_profiler_end_to_end` — вызывает `get_profiler("clickhouse")`, `profile_tables` через сервис, проверяет результат

2. **`tests/unit/test_profiling_init.py`** (или существующий файл если есть):
   - `test_get_profiler_clickhouse_returns_ch_profiler` — диспатч через get_profiler

### Что не меняем

- `PostgresProfiler` / `GreenplumProfiler` — их поведение не меняется
- Существующие PG-тесты — не должны быть задеты
- `PostgreSQL/Greenplum` deploy flow — `profile_tables` теперь работает для CH, PG-flow не затрагивается

## Риски

1. **SAMPLE на View/Dictionary/Null** — SAMPLE работает только на MergeTree.
   Для View у нас есть `base_expr(fraction=None)` → читает всю view, не
   сэмплирует. Для Dictionary — `SELECT * FROM dict` не поддерживает SAMPLE,
   но profile_tables для dictionary не имеет смысла (нет колонок-значений).
   В нашей cis_etl все таблицы MergeTree → риска нет.
2. **`percentile_cont` → `quantile`** — semantic difference: `quantile(f)(x)`
   работает на всю выборку (не ordered-set), может быть медленнее на
   больших таблицах. Митигация: `quantileExact` (точная) или
   `quantileTDigest` (приближённая, быстрее). Для профайлера выбираем
   `quantile` (default = TDigest, быстрый).
3. **`system.tables.total_rows` обновляется асинхронно** — может быть
   `NULL` для пустых таблиц или свежесозданных. `parse_meta` обрабатывает
   `None` → `reltuples=None`. UI отображает "нет оценки".
4. **Cluster desync** — `clusterAllReplicas` в `columns_sql` /
   `table_meta_sql` может попасть на реплику, где таблицы нет. Retry уже
   есть в `_exec_rows` (UNKNOWN_DATABASE retry), но `clusterAllReplicas`
   не даёт UNKNOWN_DATABASE (он молча возвращает пустой результат с
   реплик без таблицы). Принимаем — профайлер покажет пустые данные,
   не упадёт.

## Чекпойнт

После реализации обновить `_docs_/_checkpoints_/20261017_001_checkpoint.md`:
- Добавить раздел «Phase 6.2 — CH profiler»
- Пометить Phase 6 #2 как done
- Обновить таблицу сводки фаз

## Следующая сессия (после)

- Phase 6 #1: Testcontainers CH для CI
- task 20261017_002: PG/GP split analysis (отдельная задача)

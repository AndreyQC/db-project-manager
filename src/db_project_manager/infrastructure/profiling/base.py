"""Shared machinery for table-profiling SQL generators (Phase 20).

Профайлинг — чистый SQL: агрегаты считает сама БД, наружу идут только итоги.
Каждый движок получает свой генератор, без веток внутри одного запроса
(LESSONS §70). Рецепт нового движка:

1. Подкласс :class:`ProfilingSQLGenerator` — образцы ``postgres.py``
   (TABLESAMPLE) и ``greenplum.py`` (сэмпл ``random() < p``, PG 9.4);
2. Регистрация в ``get_profiler`` (``infrastructure/profiling/__init__.py``);
3. Unit-тесты SQL-строк + интеграционный прогон.

Генератор только строит строки SQL и парсит строки метаданных; исполнение —
в :class:`application.profiling_service.ProfilingService` (RO-транзакция,
statement_timeout, классификатор).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any

#: Перцентили профиля; порядок = порядок SQL-вызовов.
PERCENTILE_FRACTIONS: tuple[float, ...] = (0.01, 0.25, 0.5, 0.75, 0.99)
#: Имя поля профиля для каждой доли (p01..p99).
PERCENTILE_KEYS: dict[float, str] = {0.01: "p01", 0.25: "p25", 0.5: "p50", 0.75: "p75", 0.99: "p99"}


class ColumnCategory(str, Enum):
    """Маршрут колонки в агрегаты: определяет набор метрик в main_sql."""

    NUMERIC = "numeric"      # avg, stddev, перцентили, гистограмма
    TEMPORAL = "temporal"    # перцентили, гистограмма, top-N
    TEXT = "text"            # length, top-N, min/max
    BOOL = "bool"            # top-N
    OTHER = "other"          # только nulls + distinct (jsonb/bytea/массивы/кастом)


@dataclass(frozen=True)
class ProfilingColumn:
    """Column metadata resolved from the catalog before SQL generation."""

    name: str
    data_type: str
    nullable: bool | None = None
    category: ColumnCategory = ColumnCategory.OTHER


@dataclass(frozen=True)
class TableMeta:
    """Result of ``parse_meta``: what the planner knows about the table."""

    reltuples: int | None = None
    size_bytes: int | None = None
    stats_present: bool | None = None


def quote_ident(name: str) -> str:
    """SQL-квотирование идентификатора (двойные кавычки, удвоение внутри)."""
    return '"' + name.replace('"', '""') + '"'


def quote_literal(value: str) -> str:
    """SQL-квотирование строкового литерала (удвоение одинарных кавычек)."""
    return "'" + value.replace("'", "''") + "'"


def categorize(data_type: str) -> ColumnCategory:
    """Map a raw ``information_schema.columns.data_type`` to a metric route.

    Unknown types degrade to OTHER (nulls + distinct only) — never an error:
    a profiler must not fail on exotic column types.
    """
    dt = (data_type or "").strip().lower()
    if dt.startswith("boolean"):
        return ColumnCategory.BOOL
    if (
        "timestamp" in dt
        or dt == "date"
        or dt.startswith("time")
        or "interval" in dt
    ):
        return ColumnCategory.TEMPORAL
    if any(
        dt == p or dt.startswith(p + "(") or dt.startswith(p + " ")
        for p in ("smallint", "integer", "bigint", "numeric", "decimal", "real",
                  "double precision", "money", "smallserial", "serial", "bigserial")
    ) or dt in ("smallint", "integer", "bigint", "numeric", "decimal", "real", "money"):
        return ColumnCategory.NUMERIC
    if any(
        dt == p or dt.startswith(p + "(") or dt.startswith(p + " ")
        for p in ("character", "varchar", "text", "citext", "uuid")
    ) or dt in ("char", "name", "uuid"):
        return ColumnCategory.TEXT
    return ColumnCategory.OTHER


class ProfilingSQLGenerator(ABC):
    """Dialect-specific SQL builder for table profiling."""

    # --- catalog probes (read-only, engine-specific) ---

    @abstractmethod
    def columns_sql(self, schema: str, table: str) -> str:
        """Catalog query: column_name, data_type, nullable — in ordinal order."""

    @abstractmethod
    def table_meta_sql(self, schema: str, table: str) -> str:
        """Catalog query: reltuples, size_bytes, stats-present signal."""

    @abstractmethod
    def parse_meta(self, row: dict[str, Any]) -> TableMeta:
        """Adapt the engine-specific meta row (JSON-safe values) to TableMeta."""

    # --- table source ---

    @abstractmethod
    def base_expr(self, schema: str, table: str, fraction: float | None) -> str:
        """FROM-источник агрегатов: вся таблица (fraction=None) или сэмпл."""

    @abstractmethod
    def sample_base_expr_random(self, schema: str, table: str, fraction: float) -> str:
        """Сэмпл строковым фильтром ``random() < p`` (fallback TABLESAMPLE)."""

    # --- aggregate queries (defaults shared by PG/GP) ---

    def main_sql(self, columns: list[ProfilingColumn], base: str) -> str:
        """One-scan aggregate SELECT over the given columns (MPP-friendly).

        Route per category: NUMERIC — avg/stddev/percentiles; NUMERIC+TEMPORAL
        — min/max/percentiles; TEXT — length/min/max; OTHER — nulls+distinct
        only (jsonb has no min/max). ``__row_count`` drives null_frac.
        """
        if not columns:
            raise ValueError("main_sql: пустой список колонок")
        parts: list[str] = ['count(*) AS "__row_count"']
        for col in columns:
            q = quote_ident(col.name)
            a = f"{col.name}__"
            parts.append(f'count({q}) AS "{a}notnull"')
            parts.append(f'count(DISTINCT {q}) AS "{a}distinct"')
            if col.category == ColumnCategory.NUMERIC:
                parts.append(f'avg({q}) AS "{a}avg"')
                parts.append(f'stddev_samp({q}) AS "{a}stddev"')
            if col.category in (ColumnCategory.NUMERIC, ColumnCategory.TEMPORAL):
                parts.append(f'min({q}) AS "{a}min"')
                parts.append(f'max({q}) AS "{a}max"')
            if col.category == ColumnCategory.NUMERIC:
                # percentile_cont существует только для double precision/interval —
                # ORDER BY timestamp не приводится (живая проверка PG 18, Phase 20).
                for frac in PERCENTILE_FRACTIONS:
                    key = PERCENTILE_KEYS[frac]
                    parts.append(
                        f'percentile_cont({frac}) WITHIN GROUP (ORDER BY {q}) AS "{a}{key}"'
                    )
            if col.category == ColumnCategory.TEXT:
                parts.append(f'min({q}) AS "{a}min"')
                parts.append(f'max({q}) AS "{a}max"')
                parts.append(f'avg(length({q}::text)) AS "{a}avglen"')
                parts.append(f'max(length({q}::text)) AS "{a}maxlen"')
        body = ",\n       ".join(parts)
        return f"SELECT {body}\nFROM {base}"

    def histogram_sql(
        self, column: ProfilingColumn, lo: Any, hi: Any, buckets: int, base: str
    ) -> str | None:
        """Bucket counts between min/max; None → histogram impossible for the column.

        NUMERIC — ``width_bucket``; TEMPORAL — epoch-арифметика
        (``width_bucket``/``percentile_cont`` не принимают timestamp). Bounds
        приходят сырыми значениями из main-агрегата (строкифицированы адаптером).
        """
        q = quote_ident(column.name)
        if column.category == ColumnCategory.NUMERIC:
            lo_literal, hi_literal = _numeric_literal(lo), _numeric_literal(hi)
            if lo_literal is None or hi_literal is None or lo_literal == hi_literal:
                return None
            return (
                f'SELECT width_bucket({q}, {lo_literal}, {hi_literal}, {buckets}) AS "bucket", '
                f'count(*) AS "cnt"\nFROM {base}\nWHERE {q} IS NOT NULL\nGROUP BY 1 ORDER BY 1'
            )
        if column.category == ColumnCategory.TEMPORAL:
            lo_epoch, hi_epoch = _temporal_epoch(lo), _temporal_epoch(hi)
            if lo_epoch is None or hi_epoch is None or hi_epoch <= lo_epoch:
                return None
            width = (hi_epoch - lo_epoch) / buckets
            return (
                f'SELECT floor((extract(epoch FROM {q}) - {lo_epoch:.3f}) / {width:.9f}) AS "bucket", '
                f'count(*) AS "cnt"\nFROM {base}\nWHERE {q} IS NOT NULL\nGROUP BY 1 ORDER BY 1'
            )
        return None

    def topn_sql(self, column: ProfilingColumn, limit: int, base: str) -> str:
        """Top-N most frequent values for discrete columns."""
        q = quote_ident(column.name)
        return (
            f'SELECT {q} AS "value", count(*) AS "cnt"\nFROM {base}\n'
            f'WHERE {q} IS NOT NULL\nGROUP BY {q} ORDER BY 2 DESC, 1 ASC LIMIT {limit}'
        )


def _numeric_literal(value: Any) -> str | None:
    """Numeric min/max (JSON-safe from the adapter) → SQL literal; None if impossible."""
    if value is None:
        return None
    try:
        dec = Decimal(str(value))
    except Exception:
        return None
    if not dec.is_finite():
        return None
    return f"{dec:f}"


def _temporal_epoch(value: Any) -> float | None:
    """Temporal min/max (ISO-строка от адаптера) → epoch-секунды; None if impossible.

    Naive-значения трактуются как UTC — для корзин гистограммы важна только
    монотонность, а не абсолютная привязка.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    for fmt in (
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()

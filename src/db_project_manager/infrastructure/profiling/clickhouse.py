"""ClickHouse SQL-profiling generator (Phase 6.2).

CH 25.x dialect for ``ProfilingSQLGenerator``. Used by
``application/profiling_service.py::ProfilingService`` when
``ConnectionConfig.type == "clickhouse"`` (registered in
``infrastructure/profiling/__init__.py::get_profiler``).

CH-специфика:

* **Сэмплирование** — ``SAMPLE k`` (1/k строк), только для MergeTree family.
  Для View/Dictionary/Null SAMPLE не работает → ``sample_base_expr_random``
  через ``WHERE rand() < p``.
* **Каталог** — ``system.columns`` / ``system.tables`` / ``system.parts``,
  всегда через ``clusterAllReplicas('default', ...)`` для обхода 3-шардового
  cluster desync (см. чекпойнт 20261017_001 Phase 5.6).
* **Агрегаты** — CH не имеет ``percentile_cont`` /
  ``percentile_cont(...) WITHIN GROUP (ORDER BY ...)``; используем
  ``quantile(f)(x)`` (default = TDigest, approximate, fast).
* **TEXT-длина** — в CH ``length(x)`` работает на любом типе, приведение
  к ``::text`` не нужно. Override в ``text_length_expr``.
"""

from __future__ import annotations

from typing import Any

from db_project_manager.infrastructure.profiling.base import (
    ProfilingSQLGenerator,
    TableMeta,
    quote_ident,
    quote_literal,
)


class CHProfiler(ProfilingSQLGenerator):
    """SQL builder for ClickHouse 25.x.

    Use via ``get_profiler("clickhouse")``. Schema name = target
    database; CH has no PG-style schemas.
    """

    def columns_sql(self, schema: str, table: str) -> str:
        """``system.columns`` через ``clusterAllReplicas`` — column name,
        type, nullable. CH нет отдельного ``nullable`` флага: ``Nullable(X)``
        оборачивает тип, не nullable = тип не начинается с ``Nullable(``.
        """
        s, t = quote_literal(schema), quote_literal(table)
        return (
            "SELECT name AS column_name,\n"
            "       type AS data_type,\n"
            "       NOT startsWith(type, 'Nullable(') AS nullable\n"
            f"FROM clusterAllReplicas('default', system.columns)\n"
            f"WHERE database = {s} AND table = {t}\n"
            "ORDER BY position"
        )

    def table_meta_sql(self, schema: str, table: str) -> str:
        """``system.tables`` + LEFT JOIN ``system.parts`` для счётчика
        активных/всего партов. ``total_rows`` / ``total_bytes`` приходят
        из system.tables и обновляются асинхронно при merge — для
        пустых/свежесозданных таблиц они могут быть ``NULL``.
        """
        s, t = quote_literal(schema), quote_literal(table)
        return (
            "SELECT t.total_rows AS reltuples,\n"
            "       t.total_bytes AS size_bytes,\n"
            "       p.parts_active,\n"
            "       p.parts_total\n"
            f"FROM clusterAllReplicas('default', system.tables) t\n"
            "LEFT JOIN (\n"
            "    SELECT database,\n"
            "           table,\n"
            "           countIf(active = 1) AS parts_active,\n"
            "           count() AS parts_total\n"
            f"    FROM clusterAllReplicas('default', system.parts)\n"
            "    GROUP BY database, table\n"
            ") p ON p.database = t.database AND p.table = t.name\n"
            f"WHERE t.database = {s} AND t.name = {t}"
        )

    def parse_meta(self, row: dict[str, Any]) -> TableMeta:
        """Adapt the row from ``table_meta_sql`` to ``TableMeta``.

        ``total_rows`` (CH) ≈ ``reltuples`` (PG) — оценка количества
        строк. ``stats_present`` = True когда ``total_rows`` не None
        (т.е. merge выполнился хотя бы раз).
        """
        reltuples = _as_int_or_none(row.get("reltuples"))
        return TableMeta(
            reltuples=reltuples,
            size_bytes=_as_int_or_none(row.get("size_bytes")),
            stats_present=reltuples is not None,
        )

    def base_expr(self, schema: str, table: str, fraction: float | None) -> str:
        """Полная таблица (fraction=None) или детерминированный SAMPLE.

        ``SAMPLE k`` где ``k = max(2, round(1/fraction))``. Работает только
        для MergeTree family; на View/Dictionary SAMPLE упадёт — но
        service-level guard (видно из type Meta) выбирает для них
        ``sample_base_expr_random``.
        """
        qualified = f"{quote_ident(schema)}.{quote_ident(table)}"
        if fraction is None:
            return qualified
        k = max(2, int(round(1.0 / fraction)))
        return f"{qualified} SAMPLE {k}"

    def sample_base_expr_random(
        self, schema: str, table: str, fraction: float
    ) -> str:
        """``WHERE rand() < p`` — fallback для не-MergeTree движков
        (View, Dictionary) или когда SAMPLE недопустим.
        """
        return (
            f"{quote_ident(schema)}.{quote_ident(table)} "
            f"WHERE rand() < {fraction}"
        )

    # --- engine-specific fragments ---

    def percentile_expr(self, fraction: float, column_ref: str) -> str:
        """``quantile(f)(x)`` — aggregate function в CH (default = TDigest).

        Точность: для median/квантилей даёт приближённый, но быстрый
        результат. Если нужна точная медиана — ``quantileExact(f)(x)``,
        но он O(N) памяти. Для профайлера приближённый вариант OK.
        """
        return f"quantile({fraction})({column_ref})"

    def text_length_expr(self, column_ref: str) -> str:
        """``length(x)`` в CH работает на любом типе (String, FixedString,
        числовые, Nullable), приведение ``::text`` не нужно.
        """
        return f"length({column_ref})"

    # --- histogram: cast Decimal → Float64 (CH widthBucket rejects Decimal) ---

    def histogram_sql(
        self, column, lo, hi, buckets: int, base: str
    ) -> str | None:
        """Bucket counts between min/max; None → histogram impossible.

        CH override: ``widthBucket`` в CH требует Int*/UInt*/Float*
        (Decimal не поддерживается — ``ILLEGAL_TYPE_OF_ARGUMENT``).
        Кастим колонку в ``Float64`` через ``toFloat64()``. Для
        TEMPORAL колонок используем epoch-подход из ``base.py``.
        """
        from db_project_manager.infrastructure.profiling.base import (
            ColumnCategory,
            _numeric_literal,
        )

        q = quote_ident(column.name)
        if column.category == ColumnCategory.NUMERIC:
            lo_literal, hi_literal = _numeric_literal(lo), _numeric_literal(hi)
            if lo_literal is None or hi_literal is None or lo_literal == hi_literal:
                return None
            return (
                f"SELECT widthBucket(toFloat64({q}), {lo_literal}, "
                f"{hi_literal}, {buckets}) AS bucket, count(*) AS cnt\n"
                f"FROM {base}\nWHERE {q} IS NOT NULL\n"
                f"GROUP BY 1 ORDER BY 1"
            )
        # TEMPORAL: тот же epoch-подход, что в base.py
        return super().histogram_sql(column, lo, hi, buckets, base)


def _as_int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None

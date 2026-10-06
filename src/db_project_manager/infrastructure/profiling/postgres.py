"""Postgres SQL-profiling generator (Phase 20).

PostgreSQL 9.5+ (проверяется на PG 18): сэмплирование — нативный
``TABLESAMPLE SYSTEM`` (страничный, экономит IO); fallback на
``random() < p`` — когда страниц мало и сэмпл пуст. Свежесть статистики —
``pg_stat_user_tables.last_analyze``.
"""

from __future__ import annotations

from typing import Any

from db_project_manager.infrastructure.profiling.base import (
    ProfilingSQLGenerator,
    TableMeta,
    quote_ident,
    quote_literal,
)


class PostgresProfiler(ProfilingSQLGenerator):
    """SQL builder for PostgreSQL (reference implementation)."""

    def columns_sql(self, schema: str, table: str) -> str:
        s, t = quote_literal(schema), quote_literal(table)
        return (
            "SELECT column_name, data_type, (is_nullable = 'YES') AS nullable\n"
            "FROM information_schema.columns\n"
            f"WHERE table_schema = {s} AND table_name = {t}\n"
            "ORDER BY ordinal_position"
        )

    def table_meta_sql(self, schema: str, table: str) -> str:
        s, t = quote_literal(schema), quote_literal(table)
        return (
            "SELECT c.reltuples AS reltuples,\n"
            "       pg_total_relation_size(c.oid) AS size_bytes,\n"
            "       GREATEST(s.last_analyze, s.last_autoanalyze) AS stats_present\n"
            "FROM pg_class c\n"
            "JOIN pg_namespace n ON n.oid = c.relnamespace\n"
            "LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid\n"
            f"WHERE n.nspname = {s} AND c.relname = {t} AND c.relkind IN ('r', 'p')"
        )

    def parse_meta(self, row: dict[str, Any]) -> TableMeta:
        reltuples = _as_int_or_none(row.get("reltuples"))
        # -1 = «никогда не анализировалась» (PG); для профиля это «нет оценки».
        if reltuples is not None and reltuples < 0:
            reltuples = None
        last_analyzed = row.get("stats_present")
        return TableMeta(
            reltuples=reltuples,
            size_bytes=_as_int_or_none(row.get("size_bytes")),
            stats_present=last_analyzed is not None,
        )

    def base_expr(self, schema: str, table: str, fraction: float | None) -> str:
        qualified = f"{self._qualified(schema, table)}"
        if fraction is None:
            return qualified
        pct = min(100.0, fraction * 100.0)
        return (
            f"(SELECT * FROM {qualified} TABLESAMPLE SYSTEM ({pct:g})) __dbpm_sample"
        )

    def sample_base_expr_random(self, schema: str, table: str, fraction: float) -> str:
        return f"(SELECT * FROM {self._qualified(schema, table)} WHERE random() < {fraction}) __dbpm_sample"

    @staticmethod
    def _qualified(schema: str, table: str) -> str:
        return f"{quote_ident(schema)}.{quote_ident(table)}"


def _as_int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None

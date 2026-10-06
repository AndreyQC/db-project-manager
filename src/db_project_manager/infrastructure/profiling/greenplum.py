"""Greenplum SQL-profiling generator (Phase 20).

GP 6.19.4 = ядро PostgreSQL 9.4.26 (LESSONS §70): ``TABLESAMPLE`` отсутствует
(spike 2026-10-06 — SyntaxError), сэмплирование — фильтр ``random() < p``
(локальный на сегментах). Свежесть статистики — факт наличия строк в
``pg_stats`` (``last_analyze`` на GP ненадёжен: spike показал 0/3 analyzed
при 455 колонках в pg_stats); предупреждения по таблицам без статистики —
``gp_toolkit.gp_stats_missing`` (информационно, ANALYZE инструмент не
запускает).
"""

from __future__ import annotations

from typing import Any

from db_project_manager.infrastructure.profiling.base import TableMeta, quote_literal
from db_project_manager.infrastructure.profiling.postgres import (
    PostgresProfiler,
    _as_int_or_none,
)


class GreenplumProfiler(PostgresProfiler):
    """SQL builder for Greenplum 6 (PG 9.4 kernel)."""

    def table_meta_sql(self, schema: str, table: str) -> str:
        s, t = quote_literal(schema), quote_literal(table)
        return (
            "SELECT c.reltuples AS reltuples,\n"
            "       pg_total_relation_size(c.oid) AS size_bytes,\n"
            "       EXISTS (SELECT 1 FROM pg_stats st\n"
            "               WHERE st.schemaname = n.nspname AND st.tablename = c.relname) AS stats_present\n"
            "FROM pg_class c\n"
            "JOIN pg_namespace n ON n.oid = c.relnamespace\n"
            f"WHERE n.nspname = {s} AND c.relname = {t} AND c.relkind IN ('r', 'p')"
        )

    def parse_meta(self, row: dict[str, Any]) -> TableMeta:
        reltuples = _as_int_or_none(row.get("reltuples"))
        if reltuples is not None and reltuples < 0:
            reltuples = None
        return TableMeta(
            reltuples=reltuples,
            size_bytes=_as_int_or_none(row.get("size_bytes")),
            stats_present=bool(row.get("stats_present")),
        )

    def base_expr(self, schema: str, table: str, fraction: float | None) -> str:
        if fraction is None:
            return self._qualified(schema, table)
        return self.sample_base_expr_random(schema, table, fraction)

    def sample_base_expr_random(self, schema: str, table: str, fraction: float) -> str:
        return (
            f"(SELECT * FROM {self._qualified(schema, table)} "
            f"WHERE random() < {fraction}) __dbpm_sample"
        )

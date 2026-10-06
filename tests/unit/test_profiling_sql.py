"""Unit-тесты SQL-генераторов профайлинга (Phase 20)."""

from __future__ import annotations

import pytest

from db_project_manager.infrastructure.profiling import get_profiler
from db_project_manager.infrastructure.profiling.base import (
    ColumnCategory,
    ProfilingColumn,
    categorize,
    quote_ident,
)
from db_project_manager.infrastructure.profiling.greenplum import GreenplumProfiler
from db_project_manager.infrastructure.profiling.postgres import PostgresProfiler


def col(name: str, category: ColumnCategory, data_type: str = "numeric") -> ProfilingColumn:
    return ProfilingColumn(name=name, data_type=data_type, category=category)


# --- categorize ---


@pytest.mark.parametrize(
    "dtype,expected",
    [
        ("integer", ColumnCategory.NUMERIC),
        ("bigint", ColumnCategory.NUMERIC),
        ("numeric(12,2)", ColumnCategory.NUMERIC),
        ("double precision", ColumnCategory.NUMERIC),
        ("real", ColumnCategory.NUMERIC),
        ("money", ColumnCategory.NUMERIC),
        ("timestamp without time zone", ColumnCategory.TEMPORAL),
        ("timestamp with time zone", ColumnCategory.TEMPORAL),
        ("date", ColumnCategory.TEMPORAL),
        ("time without time zone", ColumnCategory.TEMPORAL),
        ("interval", ColumnCategory.TEMPORAL),
        ("character varying(50)", ColumnCategory.TEXT),
        ("text", ColumnCategory.TEXT),
        ("uuid", ColumnCategory.TEXT),
        ("citext", ColumnCategory.TEXT),
        ("boolean", ColumnCategory.BOOL),
        ("jsonb", ColumnCategory.OTHER),
        ("bytea", ColumnCategory.OTHER),
        ("ARRAY", ColumnCategory.OTHER),
        ("xml", ColumnCategory.OTHER),
        ("", ColumnCategory.OTHER),
    ],
)
def test_categorize(dtype: str, expected: ColumnCategory) -> None:
    assert categorize(dtype) is expected


def test_quote_ident_escapes_quotes() -> None:
    assert quote_ident('we"ird') == '"we""ird"'


def test_get_profiler_registry() -> None:
    assert isinstance(get_profiler("postgres"), PostgresProfiler)
    assert isinstance(get_profiler("Greenplum"), GreenplumProfiler)
    with pytest.raises(ValueError, match="Профайлинг не поддерживается"):
        get_profiler("mssql")


# --- base expressions ---


def test_pg_base_full_and_sample() -> None:
    p = PostgresProfiler()
    assert p.base_expr("public", "t", None) == '"public"."t"'
    sample = p.base_expr("public", "t", 0.01)
    assert "TABLESAMPLE SYSTEM (1)" in sample
    assert "__dbpm_sample" in sample


def test_pg_random_fallback_expr() -> None:
    expr = PostgresProfiler().sample_base_expr_random("public", "t", 0.005)
    assert "WHERE random() < 0.005" in expr
    assert "__dbpm_sample" in expr


def test_gp_never_tablesample() -> None:
    g = GreenplumProfiler()
    assert "TABLESAMPLE" not in g.base_expr("s", "t", 0.01)
    assert "random() < 0.01" in g.base_expr("s", "t", 0.01)
    assert g.base_expr("s", "t", None) == '"s"."t"'


# --- main_sql ---


def test_main_sql_routes_metrics_by_category() -> None:
    p = PostgresProfiler()
    columns = [
        col("amount", ColumnCategory.NUMERIC),
        col("name", ColumnCategory.TEXT),
        col("created_at", ColumnCategory.TEMPORAL, "timestamp without time zone"),
        col("payload", ColumnCategory.OTHER),
    ]
    sql = p.main_sql(columns, '"public"."t"')
    assert 'count(*) AS "__row_count"' in sql
    assert 'count(DISTINCT "amount")' in sql
    assert 'avg("amount")' in sql
    assert "stddev_samp" in sql
    assert "percentile_cont(0.5) WITHIN GROUP (ORDER BY \"amount\")" in sql
    assert 'avg(length("name"::text))' in sql
    assert 'min("created_at")' in sql
    # percentile_cont не принимает timestamp (живая проверка PG 18)
    assert 'WITHIN GROUP (ORDER BY "created_at")' not in sql
    # OTHER: только nulls + distinct, без min/max
    assert sql.count('"payload__distinct"') == 1
    assert '"payload__min"' not in sql
    assert '"amount__min"' in sql and '"name__min"' in sql
    assert 'FROM "public"."t"' in sql


def test_main_sql_empty_columns_rejected() -> None:
    with pytest.raises(ValueError, match="пустой"):
        PostgresProfiler().main_sql([], '"public"."t"')


# --- histogram / top-N / bound literals ---


def test_histogram_sql_numeric_uses_width_bucket() -> None:
    sql = PostgresProfiler().histogram_sql(
        col("amount", ColumnCategory.NUMERIC), "0.5", "990.01", 20, '"public"."t"'
    )
    assert 'width_bucket("amount", 0.5, 990.01, 20)' in sql
    assert "GROUP BY 1 ORDER BY 1" in sql


def test_histogram_sql_temporal_uses_epoch_arithmetic() -> None:
    sql = PostgresProfiler().histogram_sql(
        col("ts", ColumnCategory.TEMPORAL, "timestamp without time zone"),
        "2026-01-01 00:00:00",
        "2026-01-02 00:00:00",
        20,
        '"public"."t"',
    )
    assert 'extract(epoch FROM "ts")' in sql
    assert "/ 4320.000000000" in sql
    assert "width_bucket" not in sql


def test_histogram_sql_impossible_cases_return_none() -> None:
    p = PostgresProfiler()
    assert p.histogram_sql(col("a", ColumnCategory.NUMERIC), "5", "5", 20, "t") is None
    assert p.histogram_sql(col("a", ColumnCategory.NUMERIC), "NaN", "5", 20, "t") is None
    assert p.histogram_sql(col("a", ColumnCategory.NUMERIC), None, "5", 20, "t") is None
    assert p.histogram_sql(col("b", ColumnCategory.OTHER), "1", "2", 20, "t") is None
    assert p.histogram_sql(
        col("ts", ColumnCategory.TEMPORAL, "timestamp without time zone"),
        "не дата", "2026-01-02", 20, "t",
    ) is None


def test_topn_sql_shape() -> None:
    sql = PostgresProfiler().topn_sql(col("region", ColumnCategory.TEXT), 10, '"public"."t"')
    assert 'SELECT "region" AS "value", count(*) AS "cnt"' in sql
    assert "ORDER BY 2 DESC, 1 ASC" in sql
    assert "LIMIT 10" in sql


# --- metadata ---


def test_pg_meta_parse() -> None:
    p = PostgresProfiler()
    meta = p.parse_meta({"reltuples": 12345, "size_bytes": 819200, "stats_present": "2026-10-06 12:00:00"})
    assert (meta.reltuples, meta.size_bytes, meta.stats_present) == (12345, 819200, True)
    meta = p.parse_meta({"reltuples": -1, "size_bytes": 0, "stats_present": None})
    assert (meta.reltuples, meta.size_bytes, meta.stats_present) == (None, 0, False)
    meta = p.parse_meta({"reltuples": "500", "size_bytes": "8192", "stats_present": "2026-01-01"})
    assert (meta.reltuples, meta.size_bytes) == (500, 8192)


def test_gp_meta_parse_uses_pg_stats_flag() -> None:
    g = GreenplumProfiler()
    assert g.parse_meta({"reltuples": 0, "size_bytes": 8192, "stats_present": True}).stats_present is True
    assert g.parse_meta({"reltuples": 0, "size_bytes": 8192, "stats_present": False}).stats_present is False


def test_meta_sql_shapes() -> None:
    pg_sql = PostgresProfiler().table_meta_sql("s", "t")
    assert "pg_stat_user_tables" in pg_sql
    gp_sql = GreenplumProfiler().table_meta_sql("s", "t")
    assert "pg_stats" in gp_sql and "pg_stat_user_tables" not in gp_sql
    for sql in (pg_sql, gp_sql):
        assert "pg_total_relation_size" in sql
        assert "relkind IN ('r', 'p')" in sql


def test_columns_sql_quotes_literals() -> None:
    sql = PostgresProfiler().columns_sql("pu'b", "t")
    assert "'pu''b'" in sql

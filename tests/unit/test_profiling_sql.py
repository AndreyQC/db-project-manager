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
from db_project_manager.infrastructure.profiling.clickhouse import CHProfiler
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
        ("citext", ColumnCategory.TEXT),
        ("boolean", ColumnCategory.BOOL),
        ("jsonb", ColumnCategory.OTHER),
        ("bytea", ColumnCategory.OTHER),
        ("ARRAY", ColumnCategory.OTHER),
        ("xml", ColumnCategory.OTHER),
        # uuid → OTHER: min/max-агрегатов для uuid в PostgreSQL нет
        # (живой прогон hrdo_ods 2026-10-07: function min(uuid) does not exist)
        ("uuid", ColumnCategory.OTHER),
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
    assert isinstance(get_profiler("clickhouse"), CHProfiler)
    with pytest.raises(ValueError, match="Профайлинг не поддерживается"):
        get_profiler("mssql")


# --- CHProfiler (Phase 6.2) ---


def test_ch_columns_sql_uses_cluster_all_replicas() -> None:
    p = CHProfiler()
    sql = p.columns_sql("cis_etl", "tr_orders")
    assert "clusterAllReplicas('default', system.columns)" in sql
    assert "database = 'cis_etl'" in sql
    assert "table = 'tr_orders'" in sql
    assert "ORDER BY position" in sql
    # CH has no is_nullable column — uses NOT startsWith(type, 'Nullable(')
    assert "startsWith(type, 'Nullable(')" in sql


def test_ch_table_meta_sql_joins_system_parts() -> None:
    p = CHProfiler()
    sql = p.table_meta_sql("cis_etl", "tr_orders")
    assert "clusterAllReplicas('default', system.tables) t" in sql
    assert "clusterAllReplicas('default', system.parts)" in sql
    assert "countIf(active = 1) AS parts_active" in sql
    assert "t.total_rows AS reltuples" in sql
    assert "t.total_bytes AS size_bytes" in sql


def test_ch_base_expr_no_sample_returns_qualified_name() -> None:
    p = CHProfiler()
    assert p.base_expr("cis_etl", "tr_orders", None) == '"cis_etl"."tr_orders"'


def test_ch_base_expr_with_sample_uses_sample_k() -> None:
    """fraction=0.1 → SAMPLE 10 (1 row out of 10)."""
    p = CHProfiler()
    expr = p.base_expr("cis_etl", "tr_orders", 0.1)
    assert expr == '"cis_etl"."tr_orders" SAMPLE 10'

    # fraction=0.5 → SAMPLE 2
    expr = p.base_expr("cis_etl", "t", 0.5)
    assert expr.endswith("SAMPLE 2")

    # fraction=0.01 → SAMPLE 100
    expr = p.base_expr("db", "t", 0.01)
    assert expr.endswith("SAMPLE 100")

    # fraction=0.99 → SAMPLE 2 (rounded, min k=2)
    expr = p.base_expr("db", "t", 0.99)
    assert expr.endswith("SAMPLE 2")


def test_ch_sample_base_expr_random_uses_where_rand() -> None:
    p = CHProfiler()
    expr = p.sample_base_expr_random("cis_etl", "t", 0.1)
    assert expr == '"cis_etl"."t" WHERE rand() < 0.1'


def test_ch_percentile_expr_uses_quantile() -> None:
    """CH override — quantile(f)(x) вместо PG percentile_cont."""
    p = CHProfiler()
    assert p.percentile_expr(0.5, "x") == "quantile(0.5)(x)"
    assert p.percentile_expr(0.95, "amount") == "quantile(0.95)(amount)"


def test_ch_text_length_expr_no_cast() -> None:
    """CH length() works on any type — no ::text cast needed."""
    p = CHProfiler()
    assert p.text_length_expr("name") == "length(name)"


def test_ch_parse_meta_handles_nulls() -> None:
    p = CHProfiler()
    # total_rows=None (empty/fresh table) → reltuples=None, stats_present=False
    m = p.parse_meta({"reltuples": None, "size_bytes": None, "parts_active": 0, "parts_total": 0})
    assert m.reltuples is None
    assert m.size_bytes is None
    assert m.stats_present is False

    # Real values
    m = p.parse_meta({"reltuples": 12345, "size_bytes": 67890, "parts_active": 2, "parts_total": 3})
    assert m.reltuples == 12345
    assert m.size_bytes == 67890
    assert m.stats_present is True


def test_ch_main_sql_uses_quantile_not_percentile_cont() -> None:
    """End-to-end: the parameterized percentile_expr flows into main_sql."""
    p = CHProfiler()
    col_id = col("id", ColumnCategory.NUMERIC, "UInt64")
    sql = p.main_sql([col_id], '"db"."t"')
    assert 'quantile(0.5)("id")' in sql
    assert 'quantile(0.99)("id")' in sql
    # No PG syntax
    assert "percentile_cont" not in sql


def test_pg_main_sql_still_uses_percentile_cont() -> None:
    """Regression: PG profiler still uses percentile_cont (default in base)."""
    p = PostgresProfiler()
    col_id = col("id", ColumnCategory.NUMERIC, "bigint")
    sql = p.main_sql([col_id], '"public"."t"')
    assert 'percentile_cont(0.5) WITHIN GROUP (ORDER BY "id")' in sql
    # No CH syntax
    assert "quantile(" not in sql


def test_pg_text_length_expr_uses_cast() -> None:
    """Regression: PG default still uses length(x::text) for non-text types."""
    p = PostgresProfiler()
    assert p.text_length_expr("name") == "length(name::text)"


def test_greenplum_inherits_pg_percentile() -> None:
    """GreenplumProfiler is a subclass of PostgresProfiler; default PG
    percentile_cont applies. (GP-specific override not required for
    percentile aggregations.)"""
    p = GreenplumProfiler()
    col_id = col("id", ColumnCategory.NUMERIC, "bigint")
    sql = p.main_sql([col_id], '"public"."t"')
    assert 'percentile_cont(0.5) WITHIN GROUP (ORDER BY "id")' in sql


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


def test_main_sql_uuid_column_no_minmax() -> None:
    """Регрессия живого прогона hrdo_ods: min/max(uuid) не существует в PG.

    uuid-колонка обязана дать только count/count distinct (OTHER-маршрут),
    иначе один GUID-столбец валит весь main-агрегат таблицы.
    """
    p = PostgresProfiler()
    sql = p.main_sql(
        [col("OrganizatsiyaGuid", ColumnCategory.OTHER, "uuid")], '"s"."t"'
    )
    assert '"OrganizatsiyaGuid__distinct"' in sql
    assert '"OrganizatsiyaGuid__min"' not in sql
    assert '"OrganizatsiyaGuid__max"' not in sql
    assert '"OrganizatsiyaGuid__avglen"' not in sql


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

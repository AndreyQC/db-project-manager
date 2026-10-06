"""Unit-тесты ProfilingService (Phase 20): гейт, порог, сэмпл, оркестрация."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import pytest

from db_project_manager.application.profiling_service import (
    ProfilingError,
    ProfilingService,
    parse_qualified,
)
from db_project_manager.domain.connection import ConnectionConfig, ProfilingSettings
from db_project_manager.domain.query import QueryResult


class RoutingAdapter:
    """Routes run_query by SQL shape; records every call."""

    def __init__(self, *, meta: dict[str, Any] | None = None, columns: list | None = None,
                 empty_first_main: bool = False) -> None:
        self.calls: list[str] = []
        self.columns = columns if columns is not None else [
            {"column_name": "id", "data_type": "integer", "nullable": False},
            {"column_name": "region", "data_type": "character varying(20)", "nullable": True},
            {"column_name": "amount", "data_type": "numeric(12,2)", "nullable": True},
            {"column_name": "payload", "data_type": "jsonb", "nullable": True},
        ]
        self.meta = meta if meta is not None else {
            "reltuples": 1000000, "size_bytes": 1048576, "stats_present": "2026-10-06 10:00:00",
        }
        self.empty_first_main = empty_first_main
        self._main_calls = 0

    @staticmethod
    def _default_main() -> dict[str, Any]:
        row: dict[str, Any] = {"__row_count": 100}
        row.update({
            "id__notnull": 100, "id__distinct": 100, "id__min": 1, "id__max": 100,
            "id__avg": "50.5", "id__stddev": "28.86", "id__p50": "50.5",
        })
        row.update({
            "region__notnull": 90, "region__distinct": 3, "region__min": "east",
            "region__max": "west", "region__avglen": "4.1", "region__maxlen": 5,
        })
        row.update({
            "amount__notnull": 95, "amount__distinct": 90, "amount__min": "0.5",
            "amount__max": "990.01", "amount__avg": "500.5", "amount__stddev": "200.1",
            "amount__p50": "500.5",
        })
        row.update({"payload__notnull": 10, "payload__distinct": 10})
        return row

    def run_query(self, sql: str, *, max_rows: int = 50, timeout_s: int = 60, readonly: bool = True) -> QueryResult:
        self.calls.append(sql)
        assert readonly is True, "профайлинг обязан исполняться в read-only режиме"
        if "information_schema.columns" in sql:
            return QueryResult(columns=[], rows=self.columns, row_count=len(self.columns))
        if "pg_class" in sql:
            return QueryResult(columns=[], rows=[self.meta], row_count=1)
        if "width_bucket" in sql:
            rows = [{"bucket": 1, "cnt": 3}, {"bucket": 2, "cnt": 7}]
            return QueryResult(columns=[], rows=rows, row_count=len(rows))
        if 'AS "value"' in sql:
            rows = [{"value": "east", "cnt": 5}]
            return QueryResult(columns=[], rows=rows, row_count=1)
        self._main_calls += 1
        if self.empty_first_main and self._main_calls == 1:
            return QueryResult(columns=[], rows=[{"__row_count": 0}], row_count=1)
        return QueryResult(columns=[], rows=[self._default_main()], row_count=1)


class FakeManager:
    """ConnectionManager stub: one config, one adapter."""

    def __init__(self, cfg: ConnectionConfig, adapter: RoutingAdapter | None = None) -> None:
        self.cfg = cfg
        self.adapter = adapter if adapter is not None else RoutingAdapter()

    def load_config(self, name: str) -> ConnectionConfig:
        return self.cfg

    @contextmanager
    def connection(self, name: str):
        yield self


def make_service(cfg: ConnectionConfig, adapter: RoutingAdapter | None = None):
    mgr = FakeManager(cfg, adapter)
    return ProfilingService(manager=mgr), mgr


def enabled_cfg(**overrides: Any) -> ConnectionConfig:
    return ConnectionConfig(
        host="h", database="d", username="u", type="postgres",
        profiling=ProfilingSettings(enabled=True, **overrides),
    )


# --- policy gate ---


def test_disabled_gate_rejects_before_any_query() -> None:
    mgr = FakeManager(ConnectionConfig(host="h", database="d", username="u"))
    service = ProfilingService(manager=mgr)
    with pytest.raises(ProfilingError, match="profiling"):
        service.profile_tables("conn", ["public.t"])
    assert mgr.adapter.calls == []


# --- happy path (full mode) ---


def test_full_profile_builds_metrics() -> None:
    service, mgr = make_service(enabled_cfg())
    profiles = service.profile_tables("conn", ["public.t"])
    assert len(profiles) == 1
    tp = profiles[0]
    assert (tp.schema_name, tp.table_name) == ("public", "t")
    assert tp.row_count == 100
    assert tp.sampled is False
    assert tp.stats_fresh is True
    assert tp.warnings == []
    cols = {c.name: c for c in tp.columns}
    assert cols["amount"].avg_value == 500.5
    assert cols["amount"].percentiles["p50"] == 500.5
    assert cols["amount"].histogram[0] == {"bucket": 1, "count": 3}
    assert cols["region"].top_values[0] == {"value": "east", "count": 5}
    assert cols["region"].null_frac == round(1 - 90 / 100, 6)
    assert cols["payload"].min_value is None
    assert cols["payload"].distinct_count == 10
    assert tp.estimated_rows == 1000000


# --- size threshold and sampling ---


def test_big_table_sampled_with_fraction_from_reltuples() -> None:
    meta = {"reltuples": 100000000, "size_bytes": 2 * 1073741824, "stats_present": True}
    service, mgr = make_service(enabled_cfg(), RoutingAdapter(meta=meta))
    tp = service.profile_tables("conn", ["public.t"])[0]
    assert tp.sampled is True
    sample_calls = [c for c in mgr.adapter.calls if "TABLESAMPLE" in c]
    assert sample_calls and "TABLESAMPLE SYSTEM (0.1)" in sample_calls[0]


def test_no_stats_warning_and_default_fraction() -> None:
    meta = {"reltuples": -1, "size_bytes": 2 * 1073741824, "stats_present": None}
    service, mgr = make_service(enabled_cfg(), RoutingAdapter(meta=meta))
    tp = service.profile_tables("conn", ["public.t"])[0]
    assert tp.stats_fresh is False
    assert any("ANALYZE" in w for w in tp.warnings)
    assert any("TABLESAMPLE SYSTEM (1)" in c for c in mgr.adapter.calls)


def test_gp_sampling_uses_random_filter_never_tablesample() -> None:
    cfg = ConnectionConfig(
        host="h", database="d", username="u", type="greenplum",
        profiling=ProfilingSettings(enabled=True),
    )
    meta = {"reltuples": 50000000, "size_bytes": 2 * 1073741824, "stats_present": True}
    service, mgr = make_service(cfg, RoutingAdapter(meta=meta))
    tp = service.profile_tables("conn", ["gp_schema.t"])[0]
    assert tp.sampled is True
    assert all("TABLESAMPLE" not in c for c in mgr.adapter.calls)
    assert any("random() < 0.002" in c for c in mgr.adapter.calls)


def test_pg_empty_sample_falls_back_to_random() -> None:
    meta = {"reltuples": 100000000, "size_bytes": 2 * 1073741824, "stats_present": True}
    service, mgr = make_service(enabled_cfg(), RoutingAdapter(meta=meta, empty_first_main=True))
    tp = service.profile_tables("conn", ["public.t"])[0]
    assert tp.sampled is True
    assert tp.row_count == 100
    assert any("TABLESAMPLE" in c for c in mgr.adapter.calls)
    assert any("random() < 0.001" in c for c in mgr.adapter.calls)
    assert any("random()" in w for w in tp.warnings)


# --- validation ---


def test_missing_table_raises() -> None:
    service, _ = make_service(enabled_cfg(), RoutingAdapter(columns=[]))
    with pytest.raises(ProfilingError, match="не найдена"):
        service.profile_tables("conn", ["public.t"])


def test_unsupported_db_type_raises() -> None:
    cfg = ConnectionConfig(
        host="h", database="d", username="u", type="mssql",
        profiling=ProfilingSettings(enabled=True),
    )
    service, _ = make_service(cfg)
    with pytest.raises(ProfilingError, match="mssql"):
        service.profile_tables("conn", ["dbo.t"])


def test_parse_qualified() -> None:
    assert parse_qualified("a.b") == ("a", "b")
    assert parse_qualified(" a . b ") == ("a", "b")
    with pytest.raises(ProfilingError, match="квалифицированным"):
        parse_qualified("nodot")
    with pytest.raises(ProfilingError, match="квалифицированным"):
        parse_qualified("a.")

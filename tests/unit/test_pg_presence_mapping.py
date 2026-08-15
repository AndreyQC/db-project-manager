"""Tests for the PG presence-stats mapping (Phase 11, step S2).

Pure unit coverage of :func:`map_presence_row` — the raw-catalog-row →
``TablePresenceStats`` normalization (SG-5). Fail-safe orientation: anything
that undermines trust in ``estimated_rows == 0`` must report ``STALE`` (SG-4).
No DB required; the SQL itself is exercised by integration tests (S8).
"""

from __future__ import annotations

from datetime import datetime, timezone

from db_project_manager.domain.safety import StatsConfidence
from db_project_manager.infrastructure.database.postgres.adapter import map_presence_row

_ANALYZED_AT = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)


def test_normal_analyzed_table_is_fresh() -> None:
    stats = map_presence_row("app", "orders", 1000.0, _ANALYZED_AT, None, 3)
    assert stats.object_schema == "app"
    assert stats.name == "orders"
    assert stats.estimated_rows == 1000
    assert stats.confidence is StatsConfidence.FRESH


def test_autoanalyzed_only_is_fresh() -> None:
    # last_analyze NULL but last_autoanalyze set → the table has stats.
    stats = map_presence_row("app", "orders", 42.0, None, _ANALYZED_AT, 0)
    assert stats.confidence is StatsConfidence.FRESH


def test_never_analyzed_is_stale() -> None:
    stats = map_presence_row("app", "orders", 0.0, None, None, None)
    assert stats.confidence is StatsConfidence.STALE
    assert stats.estimated_rows == 0


def test_reltuples_null_is_stale() -> None:
    stats = map_presence_row("app", "orders", None, _ANALYZED_AT, None, 0)
    assert stats.estimated_rows is None
    assert stats.confidence is StatsConfidence.STALE


def test_reltuples_minus_one_sentinel_is_stale() -> None:
    # PG writes -1 when a table was created/truncated and never analyzed since.
    stats = map_presence_row("app", "orders", -1.0, None, None, None)
    assert stats.estimated_rows == -1
    assert stats.confidence is StatsConfidence.STALE


def test_heavy_drift_is_stale() -> None:
    # 1000 estimated, 1000+ modified since analyze → estimate untrustworthy.
    stats = map_presence_row("app", "orders", 1000.0, _ANALYZED_AT, None, 1000)
    assert stats.confidence is StatsConfidence.STALE


def test_drift_on_empty_estimate_is_stale() -> None:
    # estimated 0 but 5 modifications since analyze → definitely not empty.
    stats = map_presence_row("app", "orders", 0.0, _ANALYZED_AT, None, 5)
    assert stats.confidence is StatsConfidence.STALE


def test_small_drift_is_fresh() -> None:
    stats = map_presence_row("app", "orders", 1000.0, _ANALYZED_AT, None, 42)
    assert stats.confidence is StatsConfidence.FRESH


def test_empty_analyzed_table_is_fresh_zero() -> None:
    stats = map_presence_row("app", "orders", 0.0, _ANALYZED_AT, None, 0)
    assert stats.estimated_rows == 0
    assert stats.confidence is StatsConfidence.FRESH

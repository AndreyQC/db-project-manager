"""Tests for db_project_manager.domain.safety (Phase 11, step S1).

Pure-domain coverage: presence classification matrix (SG-4, fail-safe),
``TouchedTable`` violation rule (CD-10), version relation (SG-6), pydantic
round-trips (LESSONS §28). No DB, no filesystem.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from db_project_manager.domain.safety import (
    DataPresence,
    SafetyGateVerdict,
    StatsConfidence,
    TablePresenceStats,
    TableTouchKind,
    TouchedTable,
    check_version_relation,
    classify_presence,
)


# ------------------------------------------------- presence classification


@pytest.mark.parametrize(
    ("estimated_rows", "confidence", "expected"),
    [
        (5, StatsConfidence.FRESH, DataPresence.HAS_DATA),
        (5, StatsConfidence.STALE, DataPresence.HAS_DATA),
        (5, StatsConfidence.UNKNOWN, DataPresence.HAS_DATA),
        (1_000_000, StatsConfidence.FRESH, DataPresence.HAS_DATA),
        (0, StatsConfidence.FRESH, DataPresence.EMPTY),
        (0, StatsConfidence.STALE, DataPresence.UNKNOWN),
        (0, StatsConfidence.UNKNOWN, DataPresence.UNKNOWN),
        (None, StatsConfidence.FRESH, DataPresence.UNKNOWN),
        (None, StatsConfidence.STALE, DataPresence.UNKNOWN),
        (None, StatsConfidence.UNKNOWN, DataPresence.UNKNOWN),
        (-1, StatsConfidence.FRESH, DataPresence.UNKNOWN),   # PG "never analyzed" sentinel
        (-1, StatsConfidence.STALE, DataPresence.UNKNOWN),
    ],
)
def test_classify_presence_matrix(
    estimated_rows: int | None,
    confidence: StatsConfidence,
    expected: DataPresence,
) -> None:
    stats = TablePresenceStats(
        object_schema="app", name="orders",
        estimated_rows=estimated_rows, confidence=confidence,
    )
    assert classify_presence(stats) is expected


def test_table_presence_stats_defaults() -> None:
    stats = TablePresenceStats(object_schema="app", name="orders")
    assert stats.estimated_rows is None
    assert stats.confidence is StatsConfidence.UNKNOWN


# ------------------------------------------------- TouchedTable / violation


def _touched(
    presence: DataPresence,
    covered_by: list[str] | None = None,
) -> TouchedTable:
    return TouchedTable(
        object_schema="app",
        name="orders",
        touch=TableTouchKind.CHANGED,
        estimated_rows=100,
        confidence=StatsConfidence.FRESH,
        presence=presence,
        covered_by=covered_by or [],
    )


def test_is_violation_has_data_uncovered() -> None:
    assert _touched(DataPresence.HAS_DATA).is_violation is True


def test_is_violation_has_data_covered() -> None:
    touched = _touched(DataPresence.HAS_DATA, covered_by=["2026-08-14_001_migrate.sql"])
    assert touched.covered is True
    assert touched.is_violation is False


def test_is_violation_empty_uncovered_is_not_violation() -> None:
    # CD-10: changes/recreation of empty tables are allowed.
    assert _touched(DataPresence.EMPTY).is_violation is False


def test_is_violation_unknown_uncovered_is_violation() -> None:
    # CD-7 fail-safe: stale/unknown statistics count as data.
    assert _touched(DataPresence.UNKNOWN).is_violation is True


def test_touched_table_roundtrip() -> None:
    # LESSONS §28: round-trip through pydantic, not substring matching.
    touched = _touched(DataPresence.HAS_DATA, covered_by=["a.sql"])
    restored = TouchedTable.model_validate(touched.model_dump())
    assert restored == touched
    assert restored.touch == TableTouchKind.CHANGED
    assert restored.presence == DataPresence.HAS_DATA


def test_touched_table_rejects_wrong_type() -> None:
    with pytest.raises(ValidationError):
        TouchedTable(object_schema="app", name="orders", touch="renamed", presence="maybe")


# ------------------------------------------------- version relation (SG-6)


@pytest.mark.parametrize(
    ("target", "source", "expected"),
    [
        (None, "2026.08.11.01", "proceed"),            # first deploy / no __deploy
        ("2026.08.11.01", "2026.08.11.01", "warn_same"),
        ("2026.08.11.02", "2026.08.11.01", "error_newer"),
        ("2026.08.12.01", "2026.08.11.99", "error_newer"),
        ("2026.08.10.99", "2026.08.11.01", "proceed"),
        ("2025.12.31.99", "2026.01.01.01", "proceed"),
    ],
)
def test_check_version_relation(target: str | None, source: str, expected: str) -> None:
    assert check_version_relation(target, source).value == expected


# ------------------------------------------------- verdict


def test_verdict_violations_property() -> None:
    verdict = SafetyGateVerdict(
        clean=False,
        db_type="postgres",
        source_version="2026.08.14.01",
        target_version="2026.08.13.01",
        touched=[
            _touched(DataPresence.HAS_DATA),                                       # violation
            _touched(DataPresence.HAS_DATA, covered_by=["a.sql"]),                 # covered
            _touched(DataPresence.EMPTY),                                          # empty
            _touched(DataPresence.UNKNOWN),                                        # fail-safe violation
        ],
    )
    violations = verdict.violations
    assert len(violations) == 2
    assert all(v.presence is not DataPresence.EMPTY for v in violations)


def test_verdict_roundtrip() -> None:
    verdict = SafetyGateVerdict(
        clean=True,
        db_type="greenplum",
        touched=[_touched(DataPresence.EMPTY, covered_by=["x.sql"])],
    )
    restored = SafetyGateVerdict.model_validate_json(verdict.model_dump_json())
    assert restored.clean is True
    assert restored.db_type == "greenplum"
    assert restored.touched[0].name == "orders"
    # enums serialize as plain strings (Phase 9 contract, diff models)
    assert verdict.model_dump()["touched"][0]["touch"] == "changed"

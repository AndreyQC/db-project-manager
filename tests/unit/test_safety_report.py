"""Tests for the safety-gate report renderer (Phase 11, step S4).

LESSONS §28: JSON is verified via pydantic round-trip, not substring matching.
"""

from __future__ import annotations

import json
from pathlib import Path

from db_project_manager.domain.safety import (
    DataPresence,
    SafetyGateVerdict,
    StatsConfidence,
    TableTouchKind,
    TouchedTable,
)
from db_project_manager.infrastructure.deploy.safety_report import (
    JSON_OUTPUT_NAME,
    MD_OUTPUT_NAME,
    render_safety_markdown,
    write_safety_report,
)


def _touched(
    name: str = "orders",
    presence: DataPresence = DataPresence.HAS_DATA,
    covered_by: list[str] | None = None,
    touch: TableTouchKind = TableTouchKind.CHANGED,
    rows: int | None = 5000,
    confidence: StatsConfidence = StatsConfidence.FRESH,
) -> TouchedTable:
    return TouchedTable(
        object_schema="app",
        name=name,
        touch=touch,
        estimated_rows=rows,
        confidence=confidence,
        presence=presence,
        covered_by=covered_by or [],
    )


def test_clean_report_has_no_violations_section(tmp_path: Path) -> None:
    verdict = SafetyGateVerdict(
        clean=True, db_type="postgres",
        source_version="2026.08.14.01", target_version="2026.08.13.01",
        touched=[_touched(presence=DataPresence.EMPTY, rows=0)],
    )
    md = render_safety_markdown(verdict)
    assert "Verdict: CLEAN" in md
    assert "Нарушения и рекомендации" not in md
    assert "app.orders" in md  # touched table still listed


def test_violation_report_contains_table_and_recommendation() -> None:
    verdict = SafetyGateVerdict(
        clean=False, db_type="postgres",
        source_version="2026.08.14.01", target_version=None,
        touched=[_touched()],  # has_data, uncovered
    )
    md = render_safety_markdown(verdict)
    assert "Verdict: VIOLATIONS (1)" in md
    assert "app.orders" in md
    assert "project.covers" in md          # the SG-3 recommendation
    assert "__migrations/pre/" in md
    assert "Пайплайн остановлен" in md
    assert "~5000" in md


def test_covered_table_marked_but_not_violation() -> None:
    verdict = SafetyGateVerdict(
        clean=True, db_type="postgres",
        touched=[_touched(covered_by=["2026-08-14_001_migrate.sql"])],
    )
    md = render_safety_markdown(verdict)
    assert "2026-08-14_001_migrate.sql" in md
    assert "Verdict: CLEAN" in md


def test_summary_line_for_ci() -> None:
    verdict = SafetyGateVerdict(
        clean=False, db_type="postgres", touched=[_touched(), _touched("items")]
    )
    md = render_safety_markdown(verdict)
    assert "VIOLATIONS: 2" in md
    assert "touched tables: 2" in md


def test_write_safety_report_files_created(tmp_path: Path) -> None:
    verdict = SafetyGateVerdict(clean=True, db_type="postgres", touched=[])
    out = tmp_path / "nested" / "report"   # §31: intermediate dirs created explicitly
    paths = write_safety_report(verdict, out)
    assert [p.name for p in paths] == [MD_OUTPUT_NAME, JSON_OUTPUT_NAME]
    assert (out / MD_OUTPUT_NAME).is_file()
    assert (out / JSON_OUTPUT_NAME).is_file()


def test_json_report_roundtrip(tmp_path: Path) -> None:
    # §28: parse back through pydantic instead of substring matching.
    verdict = SafetyGateVerdict(
        clean=False, db_type="greenplum",
        source_version="2026.08.14.01", target_version="2026.08.10.01",
        touched=[_touched(covered_by=["a.sql"]), _touched(name="items", rows=None)],
    )
    out = tmp_path
    write_safety_report(verdict, out)
    payload = json.loads((out / JSON_OUTPUT_NAME).read_text(encoding="utf-8"))
    payload.pop("generated_at", None)  # envelope field, not part of the model
    restored = SafetyGateVerdict.model_validate(payload)
    assert restored.clean is verdict.clean
    assert restored.db_type == "greenplum"
    assert len(restored.touched) == 2
    assert restored.touched[0].covered_by == ["a.sql"]
    assert restored.touched[1].estimated_rows is None

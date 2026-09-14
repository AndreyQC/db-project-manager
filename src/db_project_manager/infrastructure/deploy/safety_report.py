"""Safety-gate report renderer (Phase 11, S4, SG-2).

Renders a :class:`~db_project_manager.domain.safety.SafetyGateVerdict` into:

* ``safety_gate_report.md`` — human-readable, for review;
* ``safety_gate_report.json`` — machine-readable, for CI (LESSONS §28: the
  JSON is validated by round-tripping through the pydantic model, never by
  substring matching).

Design mirrors Phase 9/14 ``diff/markdown_report.py``: a pure
:func:`render_safety_markdown` (no I/O, unit-testable) plus a thin
:func:`write_safety_report` wrapper that creates both artifacts.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from db_project_manager.domain.safety import SafetyGateVerdict, TouchedTable

JSON_OUTPUT_NAME = "safety_gate_report.json"
MD_OUTPUT_NAME = "safety_gate_report.md"

#: reltuples == -1 (PG 13+) means the table has never been VACUUMed/ANALYZed —
#: the estimate is unknown, not negative. classify_presence already treats it
#: fail-safe; this keeps the human-facing text honest (Phase 15.7).
ROWS_NEVER_ANALYZED = -1

_PRESENCE_HINT = {
    "has_data": "есть данные",
    "empty": "пусто",
    "unknown": "неизвестно (stale) → считается «есть данные»",
}

_TOUCH_HINT = {
    "changed": "изменяется (hash-diff; детали уровня колонок — Phase 12)",
    "removed": "удаляется (DROP)",
}


def _fmt_rows(rows: int | None) -> str:
    if rows is None:
        return "?"
    if rows == ROWS_NEVER_ANALYZED:
        return "н/д (не ANALYZEd)"
    return f"~{rows}"


def rows_phrase(rows: int | None) -> str:
    """Human phrase for a violation line: ``~N строк`` / no-statistics wording."""
    if rows == ROWS_NEVER_ANALYZED:
        return "нет статистики (таблица не ANALYZEd)"
    if rows is None:
        return "число строк неизвестно"
    return f"~{rows} строк"


def _fmt_covered(t: TouchedTable) -> str:
    return ", ".join(t.covered_by) if t.covered_by else "—"


def render_safety_markdown(verdict: SafetyGateVerdict, generated_at: str | None = None) -> str:
    """Render a verdict to a markdown string (pure, no I/O)."""
    generated_at = generated_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    lines: list[str] = []
    lines.append("# Safety gate report (`deploy analyze`)")
    lines.append("")
    lines.append(f"_Generated:_ {generated_at}  ")
    lines.append(f"_DB type:_ {verdict.db_type}  ")
    lines.append(f"_Source version:_ {verdict.source_version or '—'}  ")
    lines.append(f"_Target version:_ {verdict.target_version or '— (первый deploy)'}")
    lines.append("")

    violations = verdict.violations
    if verdict.clean:
        lines.append("## Verdict: CLEAN")
        lines.append("")
        lines.append("Нарушений safety-gate нет: применение дельты разрешено правилами CD-10.")
        lines.append("")
    else:
        lines.append(f"## Verdict: VIOLATIONS ({len(violations)})")
        lines.append("")
        lines.append(
            "Обнаружены таблицы с данными, изменяемые дельтой без покрывающего "
            "pre-скрипта. **Пайплайн остановлен** (CD-9; правило: лучше потерять "
            "день, чем данные)."
        )
        lines.append("")

    if verdict.ignored_build_false:
        lines.append(
            f"> Примечание: {verdict.ignored_build_false} объектов исключены из "
            "анализа (project.build=false в autodoc) — изменения по ним не "
            "проверяются и не применяются."
        )
        lines.append("")

    if verdict.touched:
        lines.append("## Тронутые таблицы")
        lines.append("")
        lines.append("| Таблица | Изменение | Строки | Статистика | Данные | Покрытие |")
        lines.append("|---------|-----------|--------|------------|--------|----------|")
        for t in verdict.touched:
            table = f"{t.object_schema}.{t.name}"
            marker = " **!**" if t.is_violation else ""
            lines.append(
                f"| {table}{marker} | {t.touch.value} | {_fmt_rows(t.estimated_rows)} "
                f"| {t.confidence.value} | {t.presence.value} | {_fmt_covered(t)} |"
            )
        lines.append("")

    if violations:
        lines.append("## Нарушения и рекомендации")
        lines.append("")
        for t in violations:
            lines.append(f"### {t.object_schema}.{t.name}")
            lines.append("")
            lines.append(f"- Изменение: {_TOUCH_HINT.get(t.touch.value, t.touch.value)}")
            lines.append(
                f"- Оценка строк: {_fmt_rows(t.estimated_rows)} "
                f"(статистика: {t.confidence.value}; данные: {t.presence.value})"
            )
            lines.append(f"- Покрывающий pre-скрипт: {_fmt_covered(t) or 'отсутствует'}")
            lines.append("")
            lines.append(
                "**Рекомендация:** добавьте идемпотентный pre-скрипт в "
                "`__migrations/pre/` (имя `YYYY-MM-DD_NNN_description.sql`) и "
                f"объявите в его autodoc покрытие: `project.covers: [\"{t.object_schema}.{t.name}\"]`."
            )
            lines.append("")

    lines.append("---")
    tail = "CLEAN" if verdict.clean else f"VIOLATIONS: {len(violations)}"
    lines.append(f"_Summary (CI):_ safety-gate {tail}; touched tables: {len(verdict.touched)}")
    lines.append("")
    return "\n".join(lines)


def write_safety_report(verdict: SafetyGateVerdict, output_dir: Path) -> list[Path]:
    """Write ``safety_gate_report.{md,json}`` into *output_dir*; return paths.

    Creates the directory when missing (LESSONS §31: intermediate dirs are
    never created implicitly on Windows ``write_text``).
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    md_path = output_dir / MD_OUTPUT_NAME
    json_path = output_dir / JSON_OUTPUT_NAME
    payload = verdict.model_dump(mode="json")
    payload["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    md_path.write_text(render_safety_markdown(verdict), encoding="utf-8")
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return [md_path, json_path]

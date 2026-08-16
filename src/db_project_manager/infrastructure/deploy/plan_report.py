"""Delta-plan report renderer (Phase 12, S5; CD-12/CD-13).

Renders a :class:`~db_project_manager.domain.delta.DeltaPlan` into:

* ``plan.json`` — the exact pydantic dump (CI; round-trip validated, LESSONS §28);
* ``plan.md`` — human-readable review document: per-operation classification with
  reasons, row estimates, pre-script coverage, and a "needs pre-scripts" section
  with the recommendation (mirrors ``deploy/safety_report.py``).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from db_project_manager.domain.delta import DeltaPlan, OperationClass

JSON_OUTPUT_NAME = "plan.json"
MD_OUTPUT_NAME = "plan.md"

_CLASS_HINT = {
    OperationClass.SAFE: "safe",
    OperationClass.NEEDS_PRE: "needs-pre",
    OperationClass.BLOCKED: "BLOCKED",
}


def _fmt_rows(rows: int | None) -> str:
    return "?" if rows is None else f"~{rows}"


def render_plan_markdown(plan: DeltaPlan, generated_at: str | None = None) -> str:
    """Render a delta plan to a markdown string (pure, no I/O)."""
    generated_at = generated_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    lines: list[str] = []
    lines.append("# Deploy delta plan (`deploy plan`)")
    lines.append("")
    lines.append(f"_Generated:_ {generated_at}  ")
    lines.append(f"_DB type:_ {plan.db_type}  ")
    lines.append(f"_Source version:_ {plan.source_version or '—'}  ")
    lines.append(f"_Target version:_ {plan.target_version or '— (первый deploy)'}  ")
    lines.append(f"_Include drops:_ {'да' if plan.include_drops else 'нет'}")
    lines.append("")

    lines.append("## Сводка")
    lines.append("")
    lines.append(
        f"Операций: {len(plan.operations)} — safe: {len(plan.safe_ops)}, "
        f"needs-pre: {len(plan.needs_pre_ops)}, blocked: {len(plan.violations)}."
    )
    lines.append("")

    if plan.operations:
        lines.append("## Операции (в порядке применения)")
        lines.append("")
        lines.append("| # | Объект | Действие | Класс | Строки | Покрытие | Причина |")
        lines.append("|---|--------|----------|-------|--------|----------|---------|")
        for idx, op in enumerate(plan.operations, start=1):
            obj = f"{op.object_schema}.{op.object_name}" if op.object_schema else op.object_name
            marker = " **!**" if op.classification is OperationClass.BLOCKED else ""
            covered = ", ".join(op.covered_by) if op.covered_by else "—"
            lines.append(
                f"| {idx:03d} | {obj}{marker} | {op.action} "
                f"| {_CLASS_HINT[op.classification]} | {_fmt_rows(op.estimated_rows)} "
                f"| {covered} | {op.reason} |"
            )
        lines.append("")

    attention = plan.needs_pre_ops + plan.violations
    if attention:
        lines.append("## Требуют pre-скриптов")
        lines.append("")
        lines.append(
            "Эти операции нельзя применять автоматически по правилам безопасности "
            "(ROADMAP §7: таблицы с данными — только через покрывающий pre-скрипт)."
        )
        lines.append("")
        for op in attention:
            obj = f"{op.object_schema}.{op.object_name}" if op.object_schema else op.object_name
            lines.append(f"- **{obj}** ({op.action}, {_CLASS_HINT[op.classification]}): {op.reason}")
            if op.object_type == "table" and not op.covered_by:
                lines.append(
                    f"  - Рекомендация: добавьте pre-скрипт в `__migrations/pre/` и объявите "
                    f"покрытие `project.covers: [\"{obj}\"]`."
                )
        lines.append("")

    lines.append("---")
    tail = (
        f"safe: {len(plan.safe_ops)}; needs-pre: {len(plan.needs_pre_ops)}; "
        f"blocked: {len(plan.violations)}"
    )
    lines.append(f"_Summary (CI):_ operations: {len(plan.operations)}; {tail}")
    lines.append("")
    return "\n".join(lines)


def write_plan_report(plan: DeltaPlan, output_dir: Path) -> list[Path]:
    """Write ``plan.json`` + ``plan.md`` into *output_dir*; return the paths.

    Creates the directory when missing (LESSONS §31). The JSON is the plain model
    dump — no extra keys — so it round-trips through ``DeltaPlan.model_validate_json``.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / JSON_OUTPUT_NAME
    md_path = output_dir / MD_OUTPUT_NAME
    json_path.write_text(plan.model_dump_json(indent=2), encoding="utf-8")
    md_path.write_text(render_plan_markdown(plan), encoding="utf-8")
    return [md_path, json_path]

"""Reset report renderer (Phase 18, deploy reset).

Renders a :class:`~db_project_manager.application.schema_reset_service.ResetResult`
into:

* ``reset_report.md`` — human-readable review artifact;
* ``reset_report.json`` — machine-readable (dataclass asdict; advisory).

Design mirrors Phase 11 ``safety_report.py``: a pure
:func:`render_reset_markdown` (no I/O, unit-testable) plus a thin
:func:`write_reset_report` wrapper creating both files. The ACL insurance
snapshot (``reset_acl_snapshot.sql``) is written by the service itself BEFORE
any mutation — this report only describes what happened.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

JSON_OUTPUT_NAME = "reset_report.json"
MD_OUTPUT_NAME = "reset_report.md"

ACL_SNAPSHOT_NAME = "reset_acl_snapshot.sql"


def render_reset_markdown(result) -> str:
    """Render the human-readable reset report (pure function, no I/O).

    ``result`` is a ``ResetResult`` (typed loosely to avoid an import cycle:
    application imports this module for write_reset_report).
    """
    mode = "DRY-RUN (мутаций не было)" if result.dry_run else "ВЫПОЛНЕНО"
    lines = [
        "# deploy reset — отчёт",
        "",
        f"* Дата: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"* Режим: {mode}",
        f"* Цель: {result.target}",
        f"* Кодовая база: {result.codebase_dir} (db_type={result.db_type}, "
        f"source_version={result.source_version})",
        f"* Служебная схема (не тронута): {result.service_schema}",
        "",
        "## Схемы",
        "",
    ]
    if result.schemas_wiped:
        lines.append(f"* Content-drop (оболочка и права сохранены): "
                     f"**{', '.join(sorted(result.schemas_wiped))}**")
    if result.schemas_dropped:
        lines.append(f"* Удалены целиком (нет в кодовой базе): "
                     f"**{', '.join(sorted(result.schemas_dropped))}**")
    if not result.schemas_wiped and not result.schemas_dropped:
        lines.append("* Пользовательских схем не найдено — сброс не требовался.")

    if result.object_counts:
        lines += ["", "## Объектов по схемам (до сброса)", ""]
        for schema in sorted(result.object_counts):
            lines.append(f"* {schema}: {result.object_counts[schema]}")

    lines += ["", "## Extensions", ""]
    if result.extensions_dropped:
        lines.append(f"* Удалены (объекты лежали в сбрасываемых схемах): "
                     f"**{', '.join(sorted(result.extensions_dropped))}** — "
                     "деплой пересоздаст через CREATE EXTENSION IF NOT EXISTS")
    else:
        lines.append("* Не удалялись (не найдено в сбрасываемых схемах)")

    lines += [
        "",
        "## Служебная схема",
        "",
        "* schema_version: сохранена (forward-only не сброшен)",
        f"* script_history / script_audit_log: "
        f"{'очищены (TRUNCATE)' if result.journal_truncated else 'не тронуты'}",
        "",
        "## Артефакты",
        "",
        f"* ACL-снапшот (страховка): {result.acl_snapshot_path or '—'}",
    ]
    return "\n".join(lines) + "\n"


def write_reset_report(result, output_dir: str | Path) -> list[Path]:
    """Write reset_report.{json,md} into ``output_dir``; return the paths."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    payload = asdict(result)
    # Paths are not stable serialized values; sets are not JSON-serializable.
    for key in ("acl_snapshot_path", "codebase_dir"):
        if payload.get(key) is not None:
            payload[key] = str(payload[key])
    for key in ("report_paths",):
        if payload.get(key):
            payload[key] = [str(p) for p in payload[key]]
    for key in ("in_codebase",):
        if isinstance(payload.get(key), set):
            payload[key] = sorted(payload[key])

    json_path = output_dir / JSON_OUTPUT_NAME
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    md_path = output_dir / MD_OUTPUT_NAME
    md_path.write_text(render_reset_markdown(result), encoding="utf-8")
    return [json_path, md_path]

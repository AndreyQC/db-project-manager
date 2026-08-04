"""Markdown report generator for ``diff_report.json`` (Phase 14).

Renders a :class:`~db_project_manager.domain.diff.DiffReport` into a human-readable
markdown document for code review / CI artifacts. Closes BACKLOG P3
"Markdown-отчёт сравнения".

Design (see ``-=tasks=-/phase_14/Phase_14_vision_final.md`` §4.2):
    - One self-contained input (``diff_report.json`` is fully self-describing — it
      already carries both snapshots + summary + entries).
    - Pure function ``render_diff_markdown(report) -> str`` (no I/O); the thin
      ``write_diff_markdown`` wrapper handles file read/write + is used by the CLI
      ``db-pm compare report`` command.
    - ``changed`` entries get a collapsible ``<details>`` unified-diff block so the
      report stays readable for large schemas.

The unified-diff helper (:func:`unified_diff_text`) is also reused by the Delta Viewer
GUI (single source of truth for diff rendering).
"""

from __future__ import annotations

import difflib
from pathlib import Path

from db_project_manager.domain.diff import DiffEntry, DiffReport, DiffStatus, ObjectSnapshot
from db_project_manager.infrastructure.diff.grouping import (
    group_entries_by_status,
    schema_label,
    type_priority,
)

#: Order of statuses in the summary table.
_SUMMARY_ORDER = (
    DiffStatus.ADDED,
    DiffStatus.REMOVED,
    DiffStatus.CHANGED,
    DiffStatus.UNCHANGED,
)

#: Default output filename when ``--output`` is not given.
DEFAULT_OUTPUT_NAME = "diff_report.md"


def render_diff_markdown(report: DiffReport) -> str:
    """Render a :class:`DiffReport` to a markdown string (no I/O)."""
    lines: list[str] = []
    lines.append("# Diff report")
    lines.append("")
    lines.append(f"_Generated:_ {report.generated_at}")
    lines.append("")

    # --- source/target header ---
    src = report.source
    tgt = report.target
    lines.append(
        f"**Source:** `{src.source_ref}` ({src.source_kind.value}, `{src.db_type}`)  "
        f"generated {src.generated_at}"
    )
    lines.append(
        f"**Target:** `{tgt.source_ref}` ({tgt.source_kind.value}, `{tgt.db_type}`)  "
        f"generated {tgt.generated_at}"
    )
    lines.append("")

    # --- summary table ---
    lines.append("## Summary")
    lines.append("")
    lines.append("| status | count |")
    lines.append("| --- | --- |")
    for st in _SUMMARY_ORDER:
        lines.append(f"| {st.value} | {report.summary.get(st.value, 0)} |")
    lines.append("")

    # --- per-status sections (skip unchanged — it only appears in summary) ---
    for status in (DiffStatus.ADDED, DiffStatus.REMOVED, DiffStatus.CHANGED):
        _render_status_section(status, report, lines)

    # --- edge diff (Phase 14) ---
    _render_edges_section(report, lines)

    return "\n".join(lines).rstrip() + "\n"


def _render_edges_section(report: DiffReport, lines: list[str]) -> None:
    """Append the Edges section if any edges were added/removed (Phase 14)."""
    if not report.edge_entries:
        return

    lines.append("## Edges")
    lines.append("")
    es = report.edge_summary or {}
    lines.append(
        f"_Added edges: {es.get('added', 0)} · Removed edges: {es.get('removed', 0)}_"
    )
    lines.append("")

    # Split into added/removed for readability.
    added = [e for e in report.edge_entries if e.status is DiffStatus.ADDED]
    removed = [e for e in report.edge_entries if e.status is DiffStatus.REMOVED]

    for label, group in (("Added edges", added), ("Removed edges", removed)):
        if not group:
            continue
        lines.append(f"### {label} ({len(group)})")
        lines.append("")
        lines.append("| source | destination | relation | action |")
        lines.append("| --- | --- | --- | --- |")
        for entry in sorted(group, key=_edge_sort_key):
            edge = entry.source_edge if entry.source_edge is not None else entry.target_edge
            if edge is None:
                continue
            lines.append(
                f"| `{edge.source_object_key}` | `{edge.destination_object_key}` "
                f"| {edge.relation} | {edge.action or '—'} |"
            )
        lines.append("")


def _edge_sort_key(entry) -> tuple[str, str, str]:
    edge = entry.source_edge if entry.source_edge is not None else entry.target_edge
    if edge is None:
        return ("", "", "")
    return (edge.source_object_key, edge.destination_object_key, edge.relation)


def _render_status_section(status: DiffStatus, report: DiffReport, lines: list[str]) -> None:
    """Append a section for one status (skipped entirely if no entries)."""
    grouped = group_entries_by_status(report.entries, status)
    if not grouped:
        return

    title = status.value.capitalize()
    lines.append(f"## {title}")
    lines.append("")

    for object_type in sorted(grouped, key=type_priority):
        by_schema = grouped[object_type]
        lines.append(f"### {object_type} ({sum(len(v) for v in by_schema.values())})")
        lines.append("")
        lines.append("| schema | object | signature | rows |")
        lines.append("| --- | --- | --- | --- |")
        # Stable schema order: defined schemas alphabetical, None last.
        for schema in sorted(by_schema, key=lambda s: (s is None, s or "")):
            for entry in sorted(by_schema[schema], key=lambda e: _entry_name(e)):
                snap = _entry_snapshot(entry)
                lines.append(
                    "| {schema} | `{name}` | {sig} | {rows} |".format(
                        schema=schema_label(snap.object_schema if snap else schema),
                        name=_entry_name(entry),
                        sig=snap.object_signature if snap and snap.object_signature else "—",
                        rows=(
                            snap.estimated_rows
                            if snap
                            and snap.object_type == "table"
                            and snap.estimated_rows is not None
                            else "—"
                        ),
                    )
                )
                # Collapsible unified diff for changed entries.
                if status is DiffStatus.CHANGED and entry.source_snapshot and entry.target_snapshot:
                    diff = unified_diff_text(entry.source_snapshot, entry.target_snapshot)
                    if diff.strip():
                        lines.append("")
                        lines.append("<details><summary>DDL diff</summary>")
                        lines.append("")
                        lines.append("```diff")
                        lines.append(diff.rstrip())
                        lines.append("```")
                        lines.append("")
                        lines.append("</details>")
        lines.append("")

    lines.append("")


def unified_diff_text(source: ObjectSnapshot, target: ObjectSnapshot) -> str:
    """Return a unified diff between two snapshots' normalized SQL.

    Empty string when both sides are empty or equal (used by the markdown report and
    the Delta Viewer GUI — single source of truth for diff rendering).
    """
    src_lines = source.sql_normalized.splitlines(keepends=True)
    tgt_lines = target.sql_normalized.splitlines(keepends=True)
    # If both sides are textually equal there is nothing to show; difflib would still
    # emit a header, so short-circuit for cleaner output.
    if src_lines == tgt_lines:
        return ""
    name = source.object_name or target.object_name or "object"
    return "".join(
        difflib.unified_diff(
            src_lines,
            tgt_lines,
            fromfile=f"source/{name}",
            tofile=f"target/{name}",
            lineterm="",
        )
    )


def _entry_snapshot(entry: DiffEntry) -> ObjectSnapshot | None:
    """The 'present' snapshot of an entry (source for added; target for removed)."""
    return entry.source_snapshot if entry.source_snapshot is not None else entry.target_snapshot


def _entry_name(entry: DiffEntry) -> str:
    snap = _entry_snapshot(entry)
    return snap.object_name if snap else entry.object_key


def write_diff_markdown(
    report_or_path: DiffReport | str | Path,
    output: str | Path | None = None,
) -> Path:
    """Render a report to a markdown file on disk.

    Args:
        report_or_path: either a :class:`DiffReport` instance, or a path to a
            ``diff_report.json`` file (parsed via ``DiffReport.model_validate_json``).
        output: destination ``.md`` path. If ``None``:

            - when a path was given, writes ``diff_report.md`` next to it;
            - when a report instance was given, writes ``diff_report.md`` in the CWD.

    Returns:
        The path of the written markdown file.
    """
    if isinstance(report_or_path, DiffReport):
        report = report_or_path
        out_path = Path(output) if output else Path(DEFAULT_OUTPUT_NAME)
    else:
        src_path = Path(report_or_path)
        report = DiffReport.model_validate_json(src_path.read_text(encoding="utf-8"))
        out_path = Path(output) if output else src_path.parent / DEFAULT_OUTPUT_NAME

    # Create parent dirs explicitly — Path.write_text does not create intermediates,
    # and on Windows this fails with FileNotFoundError (LESSONS §31).
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_diff_markdown(report), encoding="utf-8")
    return out_path

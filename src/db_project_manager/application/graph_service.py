"""Application service: build a DependencyGraph from a codebase directory.

Used by:
  * CLI 'db-pm graph build' — produces .dbm_graph/ artifacts.
  * DeployValidateService — to obtain the deploy-ordered object list.

The service wires the parser (infrastructure) with the graph store
(.dbm_graph/ persistence) so callers stay free of those details.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger

from db_project_manager.domain.graph import DependencyGraph
from db_project_manager.infrastructure.graph import graph_store
from db_project_manager.infrastructure.graph.topological_sort import sort_by_type_and_topology
from db_project_manager.infrastructure.parsing.base import ObjectGraphParser
from db_project_manager.infrastructure.parsing.pg_sql_parser import PgSqlParser

#: Phase 8 overload-resolution report artifact, written at the codebase root
#: next to _qualify_report.md. A .md file, so it is excluded from SQL scanning.
OVERLOAD_REPORT_FILENAME = "_overload_resolution_report.md"


class BuildGraphService:
    """Build (and optionally persist) the dependency graph of a codebase."""

    def __init__(self, parser: ObjectGraphParser | None = None) -> None:
        self.parser = parser or PgSqlParser()

    def build(self, codebase_dir: str | Path) -> DependencyGraph:
        """Parse the codebase and return the in-memory graph (no I/O to disk).

        As a side effect, writes the overload-resolution report artifact when
        the parser found any unresolved overloaded calls (Phase 8). The graph
        itself is unaffected; the report is a transparent audit (LESSONS §36).
        """
        codebase_dir = Path(codebase_dir)
        logger.info(f"Построение графа по кодовой базе: {codebase_dir}")
        graph = self.parser.parse_directory(codebase_dir)
        logger.info(
            f"Граф построен: вершин={len(graph.vertices)}, рёбер={len(graph.edges)}"
        )
        self._write_overload_report(codebase_dir)
        return graph

    def _write_overload_report(self, codebase_dir: Path) -> None:
        """Write the overload-resolution report if the parser noted any.

        Reads ``parser._resolution_notes`` (populated during edge scanning).
        When there are unresolved overloaded calls, a markdown audit is written
        to ``<codebase>/_overload_resolution_report.md`` so the user can see
        which call edges fell back to first-wins. Resolved calls need no entry:
        they are visible in the graph directly. No notes -> no file written.
        """
        notes: list[dict[str, Any]] = getattr(self.parser, "_resolution_notes", [])
        if not notes:
            return
        report_path = codebase_dir / OVERLOAD_REPORT_FILENAME
        lines = self._render_overload_report(notes, str(codebase_dir))
        report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        logger.info(
            f"Overload resolution: {len(notes)} неразрешённых вызовов, отчёт записан в {report_path}"
        )

    @staticmethod
    def _render_overload_report(
        notes: list[dict[str, Any]], codebase: str
    ) -> list[str]:
        """Assemble the markdown lines for the overload-resolution report."""
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        lines: list[str] = [
            "# Overload Resolution Report",
            "",
            f"- Codebase: `{codebase}`",
            f"- Generated at (UTC): {now}",
            f"- Unresolved overloaded calls: {len(notes)}",
            "",
            "Вызовы перегруженных функций/процедур, аргументы которых не удалось",
            "однозначно вывести (литералы MVP — колонки и вложенные вызовы не",
            "поддерживаются). Ребро отнесено к первой перегрузке (fallback-first).",
            "Разрешённые вызовы в этот отчёт не попадают — они видны в графе напрямую.",
            "",
            "## Не разрешено (unresolved)",
            "",
            "| Routine (schema.name) | Calls total | Unresolved | Routed to object_key |",
            "|---|---:|---:|---|",
        ]
        for note in sorted(
            notes, key=lambda n: (str(n.get("schema")), str(n.get("name")))
        ):
            schema = note.get("schema") or "-"
            name = note.get("name") or "?"
            total = note.get("total_calls", 0)
            unresolved = note.get("unresolved", 0)
            routed = note.get("routed_key") or "-"
            lines.append(f"| `{schema}.{name}` | {total} | {unresolved} | `{routed}` |")
        lines.extend([
            "",
            "## Замечание",
            "",
            "MVP Phase 8 покрывает только литеральные аргументы вызовов. Вывод типа",
            "по колонкам и результатам вложенных вызовов — отложен (BACKLOG).",
        ])
        return lines

    def build_and_store(self, codebase_dir: str | Path) -> Path:
        """Build the graph and write it to <codebase>/.dbm_graph/."""
        codebase_dir = Path(codebase_dir)
        graph = self.build(codebase_dir)
        gdir = graph_store.write_graph(graph, codebase_dir)
        logger.info(f"Граф записан в: {gdir}")
        return gdir

    def deploy_order(self, codebase_dir: str | Path, *, build_only: bool = True) -> list:
        """Return vertices in deploy order (type+topology).

        Args:
            codebase_dir: codebase root to parse.
            build_only: if True (default), filter out build=false vertices —
                this is the deploy-time view (vision Q8).
        """
        graph = self.build(codebase_dir)
        if build_only:
            graph = graph.filter_build_true()
        return sort_by_type_and_topology(graph)

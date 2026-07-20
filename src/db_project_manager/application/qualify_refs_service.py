"""Qualify bare object references in a generated codebase.

Post-processor for reverse-engineer: walks every ``.sql`` file in the codebase,
finds bare (unqualified) references to known objects (tables/views in FROM/JOIN,
function/procedure calls) and prefixes them with the object's schema so the DDL
is deploy-safe regardless of ``search_path`` on the target database.

Decision log (Phase 6 vision):
  - Coverage: functions/procedures + tables/views in FROM/JOIN.
  - Tokenisation: regex on the RAW file text (preserves formatting). The normalizer
    is used only to find autodoc boundaries, not for rewriting.
  - Ambiguous names (same name in multiple schemas) are SKIPPED + logged.
  - Reserved keywords are skipped (use postgres.keywords.get_reserved()).
  - Self-references are skipped (a function calling itself is fine as-is).
  - A ``_qualify_report.md`` is written to the codebase root.
  - Each modified file's autodoc gets a ``qualify_report: [name, ...]`` field.

See LESSONS_LEARNED §35-36 for the design rationale and known limitations
(dynamic SQL, string literals, CTEs — regex cannot reliably handle them;
a future sqlglot-based pass can).
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger

from db_project_manager.application.graph_service import BuildGraphService
from db_project_manager.domain.graph import DependencyGraph
from db_project_manager.infrastructure.database.postgres.keywords import get_reserved
from db_project_manager.infrastructure.sql.autodoc import (
    MARKER_CLOSE,
    extract_header,
    update_header,
)

#: Object types whose references can be qualified. Excludes schema, extension,
#: database_setting (global or non-callable objects).
QUALIFIABLE_TYPES: frozenset[str] = frozenset({
    "table", "view", "materialized_view", "function", "procedure", "sequence",
})

#: Match a bare function/procedure call: ``name(`` not preceded by '.' or another
#: word char. The negative lookbehind avoids matching ``schema.name(`` and
#: composite names like ``my_col(`` which we'd otherwise mangle.
_FUNC_CALL_RE = re.compile(r"(?<![.\w])(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\(")

#: Match a bare table/view name after FROM or JOIN keywords. The name must NOT be
#: followed by '.' (already qualified) or '(' (function call, handled above).
_FROM_JOIN_RE = re.compile(
    r"\b(?:from|join)\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\b(?!\s*[.(])",
    re.IGNORECASE,
)

#: Skip dirs when walking the codebase (mirrors pg_sql_parser._SKIP_DIRS).
_SKIP_DIRS = frozenset({".dbm_graph", ".git", ".venv", "__pycache__", "node_modules"})

#: Report file name (written to codebase root).
REPORT_FILE = "_qualify_report.md"


@dataclass
class QualifyChange:
    """One qualified reference in one file."""

    file: str  # posix-relative path
    object_type: str
    object_name: str
    qualified_refs: list[str]


@dataclass
class QualifyReport:
    """Collected results of a qualify-refs run."""

    codebase: str
    generated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    changes: list[QualifyChange] = field(default_factory=list)
    #: ambiguous[name] = {schema1, schema2, ...}
    ambiguous: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    #: ambiguous_locations[name] = {file paths where it appeared}
    ambiguous_locations: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    reserved_skipped: int = 0
    files_scanned: int = 0


class QualifyRefsService:
    """Qualify bare references in a codebase using the dependency graph."""

    def __init__(self, graph_service: BuildGraphService | None = None) -> None:
        self.graph_service = graph_service or BuildGraphService()
        # Cache reserved keywords lowercased for O(1) token check.
        # Some entries in keywords.yaml parse to non-strings (True/False/None via
        # YAML's YES/NO/ON/OFF barewords); filter to str only.
        self._reserved_lower: frozenset[str] = frozenset(
            str(k).lower() for k in get_reserved() if isinstance(k, str)
        )

    def run(self, codebase_dir: str | Path, *, dry_run: bool = False) -> QualifyReport:
        """Walk the codebase, qualify bare refs, write ``_qualify_report.md``.

        Args:
            codebase_dir: Root of the generated codebase (e.g. ``<output>/<db>``).
            dry_run: When True, scan + build the report but do NOT modify files.
                The report file itself is still written.
        """
        codebase_dir = Path(codebase_dir)
        if not codebase_dir.is_dir():
            raise QualifyRefsError(f"Codebase directory not found: {codebase_dir}")

        logger.info(f"Qualify-refs: построение графа для {codebase_dir}")
        graph = self.graph_service.build(codebase_dir)
        unique, ambiguous = self._build_name_to_schema_index(graph)
        logger.info(
            f"Qualify-refs: уникальных имён={len(unique)}, "
            f"ambiguous={len(ambiguous)}, вершин={len(graph.vertices)}"
        )

        report = QualifyReport(codebase=str(codebase_dir))

        for sql_file in self._iter_sql_files(codebase_dir):
            report.files_scanned += 1
            self._process_file(sql_file, codebase_dir, graph, unique, ambiguous, report, dry_run=dry_run)

        if not dry_run:
            self._write_report(codebase_dir, report)
            logger.info(
                f"Qualify-refs: файлов просканировано={report.files_scanned}, "
                f"изменено={len(report.changes)}, ambiguous={len(report.ambiguous)}"
            )
        else:
            logger.info("Qualify-refs (dry-run): отчёт собран, файлы не изменялись")

        return report

    # --- index ---

    def _build_name_to_schema_index(
        self, graph: DependencyGraph
    ) -> tuple[dict[str, str], set[str]]:
        """Build {bare_name -> schema} for unambiguous names; ambiguous -> set."""
        by_name: dict[str, set[str]] = defaultdict(set)
        for v in graph.vertices.values():
            if not v.object_name or v.object_type not in QUALIFIABLE_TYPES:
                continue
            schema = v.object_schema or "public"
            by_name[v.object_name.lower()].add(schema)
        unique: dict[str, str] = {}
        ambiguous: set[str] = set()
        for name, schemas in by_name.items():
            if len(schemas) == 1:
                unique[name] = next(iter(schemas))
            else:
                ambiguous.add(name)
        return unique, ambiguous

    # --- per-file processing ---

    def _process_file(
        self,
        sql_file: Path,
        codebase_root: Path,
        graph: DependencyGraph,
        unique: dict[str, str],
        ambiguous: set[str],
        report: QualifyReport,
        *,
        dry_run: bool,
    ) -> None:
        raw = sql_file.read_text(encoding="utf-8-sig")
        header = extract_header(raw)
        # Only process files with autodoc — those are the ones we generated.
        if header is None:
            return
        obj = header.get("object") or {}
        self_name = str(obj.get("object_name", "")).lower()
        self_type = str(obj.get("object_type", ""))

        # Operate only on the SQL body (after the comment block), not on the
        # autodoc YAML which itself lists object names.
        body_start = self._body_start_offset(raw)
        if body_start is None:
            return
        body = raw[body_start:]

        changes_in_file: list[str] = []
        new_body = body
        # Collect edits on body, then apply by re-rendering with the schema prefix.
        # We do a single pass with both regexes, tracking positions to avoid
        # overlapping edits.
        edits: list[tuple[int, int, str, str]] = []  # (start, end, original, replacement)

        for rx in (_FUNC_CALL_RE, _FROM_JOIN_RE):
            for m in rx.finditer(body):
                name = m.group("name")
                name_lower = name.lower()
                start, end = m.start("name"), m.end("name")

                # Skip reserved keywords.
                if name_lower in self._reserved_lower:
                    report.reserved_skipped += 1
                    continue
                # Skip self-reference (function calling itself).
                if name_lower == self_name and self_type in {"function", "procedure", "view", "materialized_view"}:
                    continue
                # Skip ambiguous.
                if name_lower in ambiguous:
                    report.ambiguous[name_lower].add("?")  # schemas filled below
                    report.ambiguous_locations[name_lower].add(sql_file.as_posix())
                    continue
                schema = unique.get(name_lower)
                if schema is None:
                    continue
                # Skip if already qualified (preceding char is '.').
                # finditer with negative lookbehind already handles this, but
                # double-check defensively.
                if start > 0 and body[start - 1] == ".":
                    continue
                replacement = f"{schema}.{name}"
                edits.append((start, end, name, replacement))
                if name not in changes_in_file:
                    changes_in_file.append(name)

        if not edits:
            return

        # Apply edits right-to-left to keep indices valid.
        edits_sorted = sorted(edits, key=lambda e: e[0], reverse=True)
        for start, end, _orig, repl in edits_sorted:
            new_body = new_body[:start] + repl + new_body[end:]

        # Track ambiguous schemas from the graph (best-effort: find all schemas
        # containing this name). Done once at report time in _write_report.
        if changes_in_file:
            report.changes.append(
                QualifyChange(
                    file=sql_file.relative_to(codebase_root).as_posix(),
                    object_type=self_type,
                    object_name=obj.get("object_name", ""),
                    qualified_refs=changes_in_file,
                )
            )
            if not dry_run:
                new_raw = raw[:body_start] + new_body
                # Record the change in the autodoc header.
                def _add_qualify(metadata: dict[str, Any]) -> None:
                    metadata.setdefault("qualify_report", []).extend(changes_in_file)

                new_raw = update_header(new_raw, _add_qualify)
                sql_file.write_text(new_raw, encoding="utf-8")
                logger.debug(f"Qualify-refs: изменён {sql_file.name}: {changes_in_file}")

    @staticmethod
    def _body_start_offset(raw: str) -> int | None:
        """Return the offset where SQL body starts (after the autodoc block end).

        The autodoc block ends with ``[[autodoc-yaml]>]`` then a ``*/`` closer.
        Returns the index after the closer, or None if no marker.
        """
        if MARKER_CLOSE not in raw:
            return None
        end = raw.index(MARKER_CLOSE) + len(MARKER_CLOSE)
        tail = raw[end:]
        comment_close = tail.find("*/")
        if comment_close != -1:
            return end + comment_close + 2
        return end

    # --- report writing ---

    def _write_report(self, codebase_root: Path, report: QualifyReport) -> None:
        """Render _qualify_report.md and write to codebase root."""
        # Fill ambiguous schemas from the graph (re-scan for completeness).
        if report.ambiguous:
            graph = self.graph_service.build(codebase_root)
            by_name: dict[str, set[str]] = defaultdict(set)
            for v in graph.vertices.values():
                if v.object_name and v.object_type in QUALIFIABLE_TYPES:
                    by_name[v.object_name.lower()].add(v.object_schema or "public")
            for name in report.ambiguous:
                report.ambiguous[name] = by_name.get(name, set())

        lines: list[str] = [
            "# Qualify Refs Report",
            "",
            f"Сгенерировано: {report.generated_at}",
            f"Codebase: `{report.codebase}`",
            f"Файлов просканировано: {report.files_scanned}",
            f"Файлов изменено: {len(report.changes)}",
            f"Reserved keyword skip'ов: {report.reserved_skipped}",
            "",
            "## Изменено (qualified)",
            "",
        ]
        if report.changes:
            lines.append("| Файл | Тип объекта | Qualified refs |")
            lines.append("|------|------------|----------------|")
            for ch in report.changes:
                refs = ", ".join(sorted(ch.qualified_refs))
                lines.append(f"| `{ch.file}` | {ch.object_type} | {refs} |")
        else:
            lines.append("_Нет изменений._")
        lines.append("")

        lines.extend([
            "## Пропущено: ambiguous (одинаковые имена в разных схемах)",
            "",
        ])
        if report.ambiguous:
            lines.append("| Имя | Схемы | Встречается в файлах |")
            lines.append("|-----|-------|----------------------|")
            for name in sorted(report.ambiguous):
                schemas = ", ".join(sorted(report.ambiguous[name]))
                files = "; ".join(sorted(report.ambiguous_locations.get(name, set())))
                lines.append(f"| `{name}` | {schemas} | {files} |")
        else:
            lines.append("_Нет ambiguous имён._")
        lines.append("")

        lines.extend([
            "## Замечание",
            "",
            "Квалификация выполняется regex-парсером. Известные ограничения:",
            "- Идентификаторы внутри строковых литералов и ``$function$`` тел могут",
            "  остаться bare — проверьте вручную, если deploy падает на функции.",
            "- Ambiguous имена требуют ручного решения (укажите схему явно).",
            "- См. `LESSONS_LEARNED.md` §35-36.",
            "",
        ])

        report_path = codebase_root / REPORT_FILE
        report_path.write_text("\n".join(lines), encoding="utf-8")
        logger.info(f"Qualify-refs: отчёт записан в {report_path}")

    @staticmethod
    def _iter_sql_files(root: Path):
        """Yield .sql files, skipping hidden/cache dirs (mirrors parser)."""
        for path in root.rglob("*.sql"):
            if any(part in _SKIP_DIRS for part in path.parts):
                continue
            yield path


class QualifyRefsError(Exception):
    """Raised on qualify-refs service errors."""

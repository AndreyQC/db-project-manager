"""Delta-plan orchestration (Phase 12, S5; CD-12/CD-13).

Turns a :class:`~db_project_manager.domain.diff.DiffReport` plus the Phase 11 safety
signals (presence stats, pre-script coverage) into an ordered, classified
:class:`~db_project_manager.domain.delta.DeltaPlan`, and writes the review artifacts:

* ``delta/NNN_<type>_<schema>_<name>.sql`` — one file per executable operation,
  ``NNN`` = position in apply order (= the codebase's ``deploy_order`` toposort;
  REMOVED objects, absent from the graph, go last, sorted by object_key);
* ``plan.json`` / ``plan.md`` — see ``infrastructure/deploy/plan_report.py``.

Artifact content by action:

* ``create`` / ``rerender`` — the object's SQL body after ``strip_autodoc``
  (LESSONS §23); a table rerender is prefixed with ``DROP TABLE IF EXISTS`` (empty
  tables may be recreated — ROADMAP §7 п.2).
* ``alter`` — deterministic SAFE-only DDL from ``alter_plan.render_alter``.
* ``drop`` — a ``DROP ... IF EXISTS`` statement; for BLOCKED drop candidates the
  statement is written **commented out** with the reason (ALT-6: never auto-drop).

Comment-only bodies are downgraded to ``skip`` (``has_executable_sql``, LESSONS §49);
no artifact is written for ``skip`` / NEEDS_PRE / BLOCKED-alter operations.
"""

from __future__ import annotations

import re
from pathlib import Path

from loguru import logger

from db_project_manager.application.graph_service import BuildGraphService
from db_project_manager.domain.delta import DeltaPlan, OperationClass, PlannedOperation
from db_project_manager.domain.diff import DiffReport, DiffStatus
from db_project_manager.domain.graph import Vertex
from db_project_manager.domain.safety import (
    DataPresence,
    TablePresenceStats,
    classify_presence,
)
from db_project_manager.infrastructure.deploy.alter_plan import (
    classify,
    quote_identifier,
    render_alter,
)
from db_project_manager.infrastructure.deploy.plan_report import write_plan_report
from db_project_manager.infrastructure.sql.autodoc import strip_autodoc
from db_project_manager.infrastructure.sql.sql_text import has_executable_sql

DELTA_DIR_NAME = "delta"

#: object_type → DROP statement kind. Types not listed (trigger needs ON <table>,
#: which the plan does not carry) fall back to a commented placeholder.
_DROP_KIND = {
    "table": "TABLE",
    "view": "VIEW",
    "materialized_view": "MATERIALIZED VIEW",
    "sequence": "SEQUENCE",
    "function": "FUNCTION",
    "procedure": "PROCEDURE",
}

_FILENAME_UNSAFE_RE = re.compile(r"[^A-Za-z0-9_]")


class DeltaService:
    """Builds a classified delta plan and writes its review artifacts."""

    def __init__(self, graph_service: BuildGraphService | None = None) -> None:
        self._graph_service = graph_service or BuildGraphService()

    # ------------------------------------------------------------- planning

    def build_plan(
        self,
        codebase_dir: str | Path,
        report: DiffReport,
        stats: dict[tuple[str | None, str], TablePresenceStats],
        coverage: dict[tuple[str | None, str], list[str]],
        *,
        db_type: str,
        source_version: str | None = None,
        target_version: str | None = None,
        include_drops: bool = False,
    ) -> DeltaPlan:
        """Classify every report entry and order the operations.

        Order: the codebase's ``deploy_order`` (toposort, build=true only) for
        objects present in the graph; REMOVED objects (not in the graph) go last,
        sorted by ``object_key``. Entries for objects excluded from deploy
        (``build=false``) are skipped entirely — they are not deployed, so their
        changes are not planned.
        """
        codebase_dir = Path(codebase_dir)
        vertices = self._graph_service.deploy_order(codebase_dir, build_only=True)
        position = {v.object_key: idx for idx, v in enumerate(vertices)}

        in_graph: list[tuple[int, PlannedOperation]] = []
        removed_ops: list[PlannedOperation] = []
        skipped_not_deployed = 0
        for entry in report.entries:
            snap = entry.source_snapshot or entry.target_snapshot
            if snap is None:
                continue
            op = self._classify_entry(entry, snap, stats, coverage, include_drops=include_drops)
            if entry.status is DiffStatus.REMOVED and entry.object_key not in position:
                removed_ops.append(op)
            elif entry.object_key in position:
                in_graph.append((position[entry.object_key], op))
            else:
                skipped_not_deployed += 1
                logger.debug(
                    f"Объект {entry.object_key} исключён из деплоя (build=false) — "
                    "в план не входит."
                )
        if skipped_not_deployed:
            logger.info(
                f"{skipped_not_deployed} изменённых объектов исключены из плана "
                "(build=false)."
            )

        ordered = [op for _, op in sorted(in_graph, key=lambda pair: pair[0])]
        ordered.extend(sorted(removed_ops, key=lambda op: op.object_key))
        return DeltaPlan(
            db_type=db_type,
            source_version=source_version,
            target_version=target_version,
            operations=ordered,
            include_drops=include_drops,
        )

    @staticmethod
    def _classify_entry(
        entry,
        snap,
        stats: dict[tuple[str | None, str], TablePresenceStats],
        coverage: dict[tuple[str | None, str], list[str]],
        *,
        include_drops: bool,
    ) -> PlannedOperation:
        key = (snap.object_schema, snap.object_name)
        table_stats = stats.get(key)
        presence = classify_presence(table_stats) if table_stats is not None else DataPresence.UNKNOWN
        covered_by = list(coverage.get(key, []))
        op = classify(
            entry, presence, bool(covered_by), include_drops=include_drops
        )
        return op.model_copy(update={
            "estimated_rows": table_stats.estimated_rows if table_stats is not None else None,
            "covered_by": covered_by,
        })

    # ------------------------------------------------------------ artifacts

    def write_artifacts(
        self,
        plan: DeltaPlan,
        codebase_dir: str | Path,
        output_dir: str | Path,
    ) -> list[Path]:
        """Write ``delta/NNN_*.sql`` + ``plan.json`` + ``plan.md``; return paths.

        Mutates the plan in place: every written operation gets its ``script_file``
        set, and comment-only bodies are downgraded to ``skip`` before ``plan.json``
        is written (so the JSON reflects what is actually on disk).
        """
        codebase_dir = Path(codebase_dir)
        output_dir = Path(output_dir)
        delta_dir = output_dir / DELTA_DIR_NAME
        delta_dir.mkdir(parents=True, exist_ok=True)  # LESSONS §31

        vertices = {
            v.object_key: v
            for v in self._graph_service.deploy_order(codebase_dir, build_only=True)
        }

        written: list[Path] = []
        for idx, op in enumerate(plan.operations, start=1):
            vertex = vertices.get(op.object_key)
            content = self._artifact_content(op, vertex, codebase_dir)
            if content is None:
                if op.action not in ("skip",):
                    logger.info(
                        f"Операция {op.object_type} '{op.object_name}' "
                        f"({op.action}/{op.classification.value}) не даёт артефакта."
                    )
                continue
            filename = self._artifact_name(idx, op)
            path = delta_dir / filename
            path.write_text(content, encoding="utf-8", newline="\n")
            op.script_file = f"{DELTA_DIR_NAME}/{filename}"
            written.append(path)

        written.extend(write_plan_report(plan, output_dir))
        return written

    def _artifact_content(
        self, op: PlannedOperation, vertex: Vertex | None, codebase_dir: Path
    ) -> str | None:
        """SQL text of one operation's artifact; None = no artifact (skip)."""
        if op.action == "alter":
            if op.classification is not OperationClass.SAFE:
                return None  # needs-pre / blocked alters never get automatic DDL
            ddl = render_alter(op)
            return ddl if ddl.strip() else None

        if op.action in ("create", "rerender"):
            body = self._read_body(vertex, codebase_dir)
            if body is None:
                logger.warning(
                    f"Файл объекта не найден для {op.object_key} — артефакт не записан."
                )
                return None
            if not has_executable_sql(body):
                op.action = "skip"  # comment-only body (LESSONS §49)
                op.reason += "; исполняемого SQL нет (только комментарии)"
                return None
            if op.action == "rerender" and op.object_type == "table":
                prefix = (
                    f"DROP TABLE IF EXISTS "
                    f"{quote_identifier(op.object_schema)}.{quote_identifier(op.object_name)};\n"
                    if op.object_schema is not None
                    else f"DROP TABLE IF EXISTS {quote_identifier(op.object_name)};\n"
                )
                return prefix + body
            return body

        if op.action == "drop":
            statement = self._drop_statement(op, vertex)
            if op.classification is not OperationClass.SAFE:
                # ALT-6: blocked drop candidates are shown, never executed.
                return f"-- BLOCKED: {op.reason}\n-- {statement}\n"
            return statement + "\n"

        return None  # skip

    @staticmethod
    def _read_body(vertex: Vertex | None, codebase_dir: Path) -> str | None:
        if vertex is None or not vertex.object_source_file:
            return None
        path = codebase_dir / vertex.object_source_file
        if not path.is_file():
            return None
        return strip_autodoc(path.read_text(encoding="utf-8-sig"))

    @staticmethod
    def _drop_statement(op: PlannedOperation, vertex: Vertex | None) -> str:
        kind = _DROP_KIND.get(op.object_type)
        target = (
            f"{quote_identifier(op.object_schema)}.{quote_identifier(op.object_name)}"
            if op.object_schema is not None
            else quote_identifier(op.object_name)
        )
        if kind is None:
            return (
                f"-- DROP для типа {op.object_type} требует ручного оператора "
                f"(нет данных для конструкции); объект: {target}"
            )
        args = ""
        if kind in ("FUNCTION", "PROCEDURE") and vertex is not None and vertex.argument_types:
            args = "(" + ", ".join(
                arg.strip() for arg in vertex.argument_types.split(",") if arg.strip()
            ) + ")"
        return f"DROP {kind} IF EXISTS {target}{args};"

    @staticmethod
    def _artifact_name(idx: int, op: PlannedOperation) -> str:
        parts = [
            str(op.object_type),
            op.object_schema or "none",
            op.object_name,
        ]
        safe = (_FILENAME_UNSAFE_RE.sub("_", p) for p in parts)
        return f"{idx:03d}_" + "_".join(safe) + ".sql"

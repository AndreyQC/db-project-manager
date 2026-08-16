"""Domain models for the ALTER + Delta feature (Phase 12).

The delta layer turns a :class:`~db_project_manager.domain.diff.DiffReport` (hash-level
"something changed") into an actionable, classified plan:

- :class:`ColumnSnapshot` — one table column of one comparison side, extracted from the
  object's SQL body (ALT-1b: the SQL script is the single source of truth; columns are
  never stored in autodoc).
- :class:`ColumnDiff` / :class:`ColumnChangeKind` — what exactly changed for a column.
- :class:`PlannedOperation` — one object's outcome: an action to take plus a safety
  classification (CD-ALT-2..4).
- :class:`DeltaPlan` — all operations in apply (toposort) order, with CI-friendly
  properties.

This module is pure pydantic — no I/O, no DB, no sqlglot (same convention as
``domain/diff.py`` and ``domain/safety.py``). Serialization uses the standard pydantic
JSON machinery; all models ignore unknown keys so older reports keep parsing.

See ``_docs_/_tasks_/phase_12/Phase_12_vision_final.md`` for the normative design
(decisions ALT-1..ALT-8, classification matrix §3).
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict


class ColumnSnapshot(BaseModel):
    """One column of a table on one comparison side.

    ``type`` is the canonical type string produced by the extractor
    (``infrastructure/diff/columns.py``): sqlglot-rendered, lower-cased, type aliases
    collapsed (``integer`` == ``int4``) so both sides compare cleanly. ``comment`` is
    deliberately absent in v1: ``normalize_sql`` hashes only the first statement, so
    ``COMMENT ON`` lines are invisible to the diff anyway (see Phase 12 plan, S2 note).
    """

    model_config = ConfigDict(extra="ignore")

    name: str
    type: str
    nullable: bool
    default: str | None = None


class ColumnChangeKind(str, Enum):
    """What changed for a single column between the two sides.

    Direction is relative to the *source* (codebase) side: ``ADDED`` = present in source,
    missing in target. A column rename is NOT detected — it manifests as DROPPED + ADDED
    (ALT-3, deliberate).
    """

    ADDED = "added"
    DROPPED = "dropped"
    TYPE_CHANGED = "type_changed"
    NULLABILITY_CHANGED = "nullability_changed"
    DEFAULT_CHANGED = "default_changed"
    COMMENT_CHANGED = "comment_changed"   # declared; not produced in v1 (see ColumnSnapshot)


class ColumnDiff(BaseModel):
    """One column's change record: ``source_column``/``target_column`` populated by kind
    (added → source only; dropped → target only; the rest → both)."""

    model_config = ConfigDict(extra="ignore")

    column: str
    kind: ColumnChangeKind
    source_column: ColumnSnapshot | None = None
    target_column: ColumnSnapshot | None = None


class OperationClass(str, Enum):
    """Safety classification of a planned operation (CD-ALT-2, matrix ALT-3).

    SAFE — allowed into the automatic delta (metadata-only or additive changes).
    NEEDS_PRE — legal only with a covering pre-script (``project.covers``).
    BLOCKED — never applied automatically (uncovered data-table change, auto-DROP).
    """

    SAFE = "safe"
    NEEDS_PRE = "needs_pre"
    BLOCKED = "blocked"


#: What the delta does with an object.
Action = Literal["create", "alter", "rerender", "drop", "skip"]


class PlannedOperation(BaseModel):
    """One object's planned outcome within a :class:`DeltaPlan`.

    ``action``:
      - ``create``   — new object (ADDED): execute its file body.
      - ``alter``    — ALTER TABLE rendered from safe column diffs.
      - ``rerender`` — re-execute the file body (non-table objects, or empty tables via
        DROP+CREATE handled by the artifact writer).
      - ``drop``     — explicit DROP (only with ``include_drops``; never for data tables).
      - ``skip``     — nothing to do (UNCHANGED, comment-only, etc.).

    ``reason`` is a human-readable why — surfaced in ``plan.md`` and logs.
    """

    model_config = ConfigDict(extra="ignore")

    object_key: str
    object_type: str
    object_schema: str | None = None
    object_name: str
    action: Action
    column_diffs: list[ColumnDiff] = []
    classification: OperationClass
    reason: str
    script_file: str = ""    # relative artifact path, filled by the artifact writer
    # Informational (tables): carried into plan.json / plan.md so CI and review see
    # WHY the classification is what it is without re-reading presence stats.
    estimated_rows: int | None = None
    covered_by: list[str] = []


class DeltaPlan(BaseModel):
    """The full deployment delta: operations in apply (toposort) order.

    REMOVED objects (absent from the codebase graph) go last, sorted by ``object_key``.
    """

    model_config = ConfigDict(extra="ignore")

    db_type: str
    source_version: str | None = None
    target_version: str | None = None
    operations: list[PlannedOperation] = []
    include_drops: bool = False

    @property
    def violations(self) -> list[PlannedOperation]:
        """BLOCKED operations — the pipeline must stop (exit 1 in the CLI)."""
        return [op for op in self.operations if op.classification is OperationClass.BLOCKED]

    @property
    def needs_pre_ops(self) -> list[PlannedOperation]:
        """Operations legal only with a covering pre-script."""
        return [op for op in self.operations if op.classification is OperationClass.NEEDS_PRE]

    @property
    def safe_ops(self) -> list[PlannedOperation]:
        """Operations allowed into the automatic delta."""
        return [op for op in self.operations if op.classification is OperationClass.SAFE]

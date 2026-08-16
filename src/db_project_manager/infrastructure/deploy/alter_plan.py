"""ALTER-plan classification and DDL rendering (Phase 12, step S4; CD-ALT-2..4).

Turns a :class:`~db_project_manager.domain.diff.DiffEntry` plus the Phase 11 safety
signals (data presence, pre-script coverage) into a classified
:class:`~db_project_manager.domain.delta.PlannedOperation`, and renders the SAFE
subset of table ALTERs as deterministic, fully-qualified DDL.

The classification matrix (vision_final §3, ALT-3) is deliberately conservative:

===============  ======================  =======================================
Change           Empty table             Table with data (HAS_DATA/UNKNOWN)
===============  ======================  =======================================
ADD COLUMN       (rerender path)         SAFE when nullable and (no DEFAULT or
(nullable)                               literal DEFAULT); otherwise NEEDS_PRE
DROP COLUMN      (rerender path)         NEEDS_PRE
ALTER TYPE       (rerender path)         NEEDS_PRE (always — even widenings)
NULLABILITY      (rerender path)         widening SAFE; narrowing NEEDS_PRE
DEFAULT          (rerender path)         addition SAFE; removal/replacement
                                         NEEDS_PRE
unrepresented    (rerender path)         NEEDS_PRE (ALT-2)
===============  ======================  =======================================

A rename is indistinguishable from drop+add and is classified as such (ALT-3).

Everything here is a pure function — no I/O, no adapter. DDL identifiers go through
the whitelist quoter (LESSONS §19); a name the whitelist rejects downgrades the
operation to NEEDS_PRE instead of producing injectable/unrenderable DDL.
"""

from __future__ import annotations

import re

import sqlglot
from sqlglot import exp

from db_project_manager.domain.delta import (
    ColumnChangeKind,
    ColumnDiff,
    ColumnSnapshot,
    OperationClass,
    PlannedOperation,
)
from db_project_manager.domain.diff import DiffEntry, DiffStatus
from db_project_manager.domain.safety import DataPresence

#: Whitelist for SQL identifiers we interpolate into DDL (LESSONS §19 — same rule as
#: the adapter's ``_quote_identifier`` / ``_validate_db_name``).
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: Data-table presence values that forbid automatic mutations (UNKNOWN is fail-safe).
_WITH_DATA = (DataPresence.HAS_DATA, DataPresence.UNKNOWN)

_ACTION_CREATE = "create"
_ACTION_ALTER = "alter"
_ACTION_RERENDER = "rerender"
_ACTION_DROP = "drop"
_ACTION_SKIP = "skip"


def quote_identifier(name: str) -> str:
    """Double-quote a SQL identifier; reject anything outside the whitelist.

    Raises :class:`ValueError` on invalid input — callers that handle user-shaped
    names should pre-check with :func:`is_safe_identifier` and downgrade instead.
    """
    if not _IDENTIFIER_RE.match(name):
        raise ValueError(f"Недопустимый идентификатор: {name!r}")
    return f'"{name}"'


def is_safe_identifier(name: str) -> bool:
    return bool(_IDENTIFIER_RE.match(name))


# ------------------------------------------------------------- default safety


def _is_literal_expression(expression: exp.Expression) -> bool:
    """True for expressions a table rewrite does not depend on (PG11+ fast path)."""
    if isinstance(expression, exp.Literal):
        return True
    if isinstance(expression, exp.Boolean):
        return True
    if isinstance(expression, exp.Null):
        return True
    if isinstance(expression, exp.Neg) and isinstance(expression.this, exp.Literal):
        return True
    if isinstance(expression, exp.Cast) and _is_literal_expression(expression.this):
        return True
    return False


def is_volatile_default(default: str | None) -> bool:
    """True when a column DEFAULT is NOT a plain literal (ALT-3).

    ``42``, ``'x'``, ``NULL``, ``true``, ``-1``, ``42::int`` → False (metadata-only in
    PG11+). Anything else — ``now()``/``CURRENT_TIMESTAMP``, ``uuid_generate_v4()``,
    ``nextval(...)`` — is volatile (rewrite or per-row evaluation) → True. A default we
    cannot parse at all is volatile (fail-safe).
    """
    if default is None:
        return False
    try:
        expression = sqlglot.parse_one(default, read="postgres")
    except Exception:  # noqa: BLE001 — sqlglot raises various error subclasses
        return True
    return not _is_literal_expression(expression)


# -------------------------------------------------------- column-diff matrix


def _added_column_is_safe(column: ColumnSnapshot) -> bool:
    """ALT-3: added nullable column without DEFAULT or with a literal DEFAULT."""
    if not column.nullable:
        return False  # NOT NULL additions stay manual (conservative reading of ALT-3)
    return not is_volatile_default(column.default)


def _nullability_change_is_widening(diff: ColumnDiff) -> bool:
    """NOT NULL → NULL widens (SAFE); NULL → NOT NULL narrows (NEEDS_PRE)."""
    src, tgt = diff.source_column, diff.target_column
    assert src is not None and tgt is not None
    return src.nullable and not tgt.nullable


def _default_change_is_addition(diff: ColumnDiff) -> bool:
    """Adding a DEFAULT is SAFE; removing or replacing one is NEEDS_PRE."""
    src, tgt = diff.source_column, diff.target_column
    assert src is not None and tgt is not None
    return src.default is not None and tgt.default is None


def _column_diff_class(diff: ColumnDiff) -> OperationClass:
    """Per-diff classification for a table WITH data (matrix ALT-3)."""
    if diff.kind is ColumnChangeKind.ADDED:
        column = diff.source_column
        assert column is not None
        return OperationClass.SAFE if _added_column_is_safe(column) else OperationClass.NEEDS_PRE
    if diff.kind is ColumnChangeKind.DROPPED:
        return OperationClass.NEEDS_PRE
    if diff.kind is ColumnChangeKind.TYPE_CHANGED:
        return OperationClass.NEEDS_PRE
    if diff.kind is ColumnChangeKind.NULLABILITY_CHANGED:
        return (
            OperationClass.SAFE if _nullability_change_is_widening(diff)
            else OperationClass.NEEDS_PRE
        )
    if diff.kind is ColumnChangeKind.DEFAULT_CHANGED:
        return (
            OperationClass.SAFE if _default_change_is_addition(diff)
            else OperationClass.NEEDS_PRE
        )
    # COMMENT_CHANGED is declared but not produced in v1 (see domain.delta).
    return OperationClass.NEEDS_PRE


def classify_column_diffs(
    diffs: list[ColumnDiff], *, has_data: bool
) -> tuple[OperationClass, str]:
    """Classify a CHANGED table's column diffs (empty `has_data` is the caller's
    rerender path — this function is only meaningful for data tables).

    Any NEEDS_PRE diff makes the whole operation NEEDS_PRE; empty diffs with columns
    available = "unrepresented" change (constraints etc., ALT-2) = NEEDS_PRE.
    """
    if not has_data:  # pragma: no cover — defensive; caller rerenders empty tables
        return OperationClass.SAFE, "пустая таблица"
    if not diffs:
        return (
            OperationClass.NEEDS_PRE,
            "изменение вне колонок (констрейнты/индексы/прочее) — структурный diff "
            "уровня колонок его не представляет (ALT-2)",
        )
    reasons: list[str] = []
    for diff in diffs:
        if _column_diff_class(diff) is OperationClass.NEEDS_PRE:
            reasons.append(_describe_diff(diff))
    if reasons:
        return OperationClass.NEEDS_PRE, "; ".join(reasons)
    return OperationClass.SAFE, "только безопасные изменения колонок (ALT-3)"


def _describe_diff(diff: ColumnDiff) -> str:
    kind = diff.kind.value
    if diff.kind is ColumnChangeKind.ADDED:
        column = diff.source_column
        volatile = column is not None and is_volatile_default(column.default)
        if column is not None and not column.nullable:
            return f"колонка {diff.column}: ADD с NOT NULL — вне safe-матрицы"
        if volatile:
            return f"колонка {diff.column}: ADD с volatile DEFAULT (rewrite)"
        return f"колонка {diff.column}: {kind}"
    if diff.kind is ColumnChangeKind.NULLABILITY_CHANGED:
        widening = _nullability_change_is_widening(diff)
        narrowing_txt = "" if widening else " (сужение: SET NOT NULL)"
        return f"колонка {diff.column}: {kind}{narrowing_txt}"
    if diff.kind is ColumnChangeKind.DEFAULT_CHANGED:
        addition = _default_change_is_addition(diff)
        detail = " (добавление)" if addition else " (удаление/замена)"
        return f"колонка {diff.column}: {kind}{detail}"
    return f"колонка {diff.column}: {kind}"


# -------------------------------------------------------- entry classification


def classify(
    entry: DiffEntry,
    presence: DataPresence,
    covered: bool,
    *,
    include_drops: bool = False,
) -> PlannedOperation:
    """Classify one DiffEntry into a PlannedOperation (status-level matrix).

    ``presence`` is the Phase 11 classification for the object's table (EMPTY for
    non-tables — they have no data path); ``covered`` — a pre-script declared this
    table in ``project.covers``.
    """
    snap = entry.source_snapshot or entry.target_snapshot
    assert snap is not None, "DiffEntry carries at least one snapshot"
    base = {
        "object_key": entry.object_key,
        "object_type": snap.object_type,
        "object_schema": snap.object_schema,
        "object_name": snap.object_name,
    }

    if entry.status is DiffStatus.UNCHANGED:
        return PlannedOperation(**base, action=_ACTION_SKIP,
                                classification=OperationClass.SAFE, reason="без изменений")

    if entry.status is DiffStatus.ADDED:
        return PlannedOperation(**base, action=_ACTION_CREATE,
                                classification=OperationClass.SAFE,
                                reason="новый объект (ROADMAP §7 п.3)")

    if entry.status is DiffStatus.REMOVED:
        if not include_drops:
            return PlannedOperation(
                **base, action=_ACTION_DROP, classification=OperationClass.BLOCKED,
                reason="объекта нет в кодовой базе; авто-DROP запрещён (ALT-6, "
                       "требуется явный --include-drops)",
            )
        if snap.object_type == "table" and presence in _WITH_DATA:
            return PlannedOperation(
                **base, action=_ACTION_DROP, classification=OperationClass.BLOCKED,
                reason="DROP таблицы с данными — только через pre-скрипт (ALT-6)",
            )
        return PlannedOperation(**base, action=_ACTION_DROP,
                                classification=OperationClass.SAFE,
                                reason="явный --include-drops")

    # --- CHANGED ---
    if snap.object_type != "table":
        return PlannedOperation(**base, action=_ACTION_RERENDER,
                                classification=OperationClass.SAFE,
                                reason="не-табличный объект переприменяется целиком "
                                       "(ROADMAP §7 п.4)")

    if presence not in _WITH_DATA:
        return PlannedOperation(**base, action=_ACTION_RERENDER,
                                classification=OperationClass.SAFE,
                                column_diffs=entry.column_diffs,
                                reason="таблица пуста — пересоздание разрешено "
                                       "(ROADMAP §7 п.2)")

    if entry.columns_unavailable:
        return _needs_pre_or_blocked(
            base, covered, entry.column_diffs,
            "структурная информация о колонках недоступна (columns=None) — fail-safe",
        )

    op_class, reason = classify_column_diffs(entry.column_diffs, has_data=True)
    if op_class is OperationClass.SAFE:
        if not _renderable_identifiers(entry.column_diffs):
            return _needs_pre_or_blocked(
                base, covered, entry.column_diffs,
                "имя колонки вне whitelist идентификаторов — авто-DDL не генерируем (§19)",
            )
        return PlannedOperation(**base, action=_ACTION_ALTER,
                                classification=OperationClass.SAFE,
                                column_diffs=entry.column_diffs, reason=reason)
    return _needs_pre_or_blocked(base, covered, entry.column_diffs, reason)


def _needs_pre_or_blocked(
    base: dict, covered: bool, column_diffs: list[ColumnDiff], reason: str
) -> PlannedOperation:
    if covered:
        return PlannedOperation(**base, action=_ACTION_ALTER,
                                classification=OperationClass.NEEDS_PRE,
                                column_diffs=column_diffs,
                                reason=f"{reason}; таблица покрыта pre-скриптом")
    return PlannedOperation(**base, action=_ACTION_ALTER,
                            classification=OperationClass.BLOCKED,
                            column_diffs=column_diffs,
                            reason=f"{reason}; покрывающего pre-скрипта нет")


def _renderable_identifiers(diffs: list[ColumnDiff]) -> bool:
    """Every column a safe ALTER would touch must pass the identifier whitelist."""
    for diff in diffs:
        if not is_safe_identifier(diff.column):
            return False
        if diff.source_column is not None and not is_safe_identifier(diff.source_column.name):
            return False
    return True


# ---------------------------------------------------------------- DDL rendering


def render_alter(op: PlannedOperation) -> str:
    """Render the SAFE subset of a table's ALTER statements ("" when not renderable).

    Only produces DDL for operations the classifier marked SAFE — NEEDS_PRE/BLOCKED
    never get automatic DDL (the human writes the pre-script). Every identifier is
    whitelist-checked and double-quoted; the table is fully-qualified (LESSONS §34/§35).
    """
    if op.classification is not OperationClass.SAFE or op.action != _ACTION_ALTER:
        return ""
    if op.object_schema is None:
        return ""
    if not (is_safe_identifier(op.object_schema) and is_safe_identifier(op.object_name)):
        return ""
    table = f"{quote_identifier(op.object_schema)}.{quote_identifier(op.object_name)}"

    statements: list[str] = []
    for diff in op.column_diffs:
        if diff.kind is ColumnChangeKind.ADDED:
            column = diff.source_column
            if column is None or not is_safe_identifier(column.name):
                continue
            stmt = (
                f"ALTER TABLE {table} ADD COLUMN "
                f"{quote_identifier(column.name)} {column.type}"
            )
            if column.default is not None:
                stmt += f" DEFAULT {column.default}"
            statements.append(stmt + ";")
        elif diff.kind is ColumnChangeKind.NULLABILITY_CHANGED:
            if _nullability_change_is_widening(diff) and is_safe_identifier(diff.column):
                statements.append(
                    f"ALTER TABLE {table} ALTER COLUMN "
                    f"{quote_identifier(diff.column)} DROP NOT NULL;"
                )
        elif diff.kind is ColumnChangeKind.DEFAULT_CHANGED:
            column = diff.source_column
            if (
                _default_change_is_addition(diff)
                and column is not None
                and column.default is not None
                and is_safe_identifier(diff.column)
            ):
                statements.append(
                    f"ALTER TABLE {table} ALTER COLUMN "
                    f"{quote_identifier(diff.column)} SET DEFAULT {column.default};"
                )
        # DROPPED / TYPE_CHANGED / narrowing never reach a SAFE operation — skipped.
    return "\n".join(statements)

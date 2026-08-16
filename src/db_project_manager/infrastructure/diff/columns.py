"""Column extraction from a table's SQL body (Phase 12, ALT-1b).

The SQL script is the **single source of truth** for table columns: nothing is stored in
autodoc, so the code-first workflow (hand-edited ``CREATE TABLE``) can never drift from
the metadata. Both comparison sides go through this same extractor — the DIR side reads
the codebase files, the DB side reads the reverse-engineer's canonical DDL output.

``extract_columns`` parses the first statement of the body (a table file is
``CREATE TABLE ...;`` followed by optional ``COMMENT ON`` statements, which are not
columns) and maps each ``ColumnDef`` to a normalized
:class:`~db_project_manager.domain.delta.ColumnSnapshot`:

- ``type`` — sqlglot-rendered canonical type string. sqlglot collapses most PostgreSQL
  synonyms by itself (``integer``/``int``/``int4`` → ``INT``,
  ``character varying`` → ``VARCHAR``, ``timestamp with time zone`` → ``TIMESTAMPTZ``);
  the few it does not (``BPCHAR`` vs ``CHAR``) are handled by :data:`_TYPE_ALIASES`.
- ``nullable`` — ``False`` iff an inline ``NOT NULL`` column constraint is present
  (table-level constraints don't affect this).
- ``default`` — rendered default expression text (e.g. ``nextval('app.t_id_seq'::REGCLASS)``),
  ``None`` when absent.

Any parse failure, non-``Create`` statement, or a ``Create`` without a column list
(``PARTITION OF`` children, ``AS SELECT``) returns ``None`` — "columns unavailable" —
which downstream classification treats fail-safe (needs-pre when the table has data).

``normalize_sql`` / ``sql_hash`` are deliberately untouched (hash-stability invariant of
Phase 12): this module does its own ``parse_one``.
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp

from db_project_manager.domain.delta import ColumnSnapshot
from db_project_manager.infrastructure.diff.normalize_sql import DEFAULT_DIALECT

#: Type synonyms sqlglot does NOT collapse on its own (base name, before modifiers).
#: Keys and values are lower-case rendered base names; modifiers are preserved as-is.
_TYPE_ALIASES = {
    "bpchar": "char",   # RE writes udt_name bpchar; hand-written DDL says char
}


def canonical_type(dtype: exp.DataType, *, dialect: str = DEFAULT_DIALECT) -> str:
    """Render a sqlglot DataType to a canonical, comparison-stable string.

    Lower-cased, alias-collapsed base name, whitespace-free modifiers. Both comparison
    sides run through this function, so any deterministic form would be symmetric —
    the canonicalization mainly keeps the string short and human-readable in reports.
    """
    rendered = dtype.sql(dialect=dialect).lower()
    base, sep, rest = rendered.partition("(")
    base = _TYPE_ALIASES.get(base.strip(), base.strip())
    return f"{base}{sep}{rest}".replace(" ", "")


def extract_columns(body: str, *, dialect: str = DEFAULT_DIALECT) -> list[ColumnSnapshot] | None:
    """Extract column snapshots from a ``CREATE TABLE`` body.

    Returns ``None`` when the columns cannot be reliably determined (empty body, parse
    error, not a CREATE statement, no column list — e.g. ``PARTITION OF`` — or a
    ColumnDef without a type). ``None`` means "unavailable", never "no columns": an
    empty column list also yields ``None`` because a real table without columns is not
    a meaningful state, and pretending we know it would be unsound.
    """
    if not body or not body.strip():
        return None
    try:
        tree = sqlglot.parse_one(body, read=dialect)
    except Exception:  # noqa: BLE001 — sqlglot raises various error subclasses
        return None
    if not isinstance(tree, exp.Create):
        return None
    schema = tree.this if isinstance(tree.this, exp.Schema) else None
    if schema is None:
        return None

    columns: list[ColumnSnapshot] = []
    for item in schema.expressions:
        if not isinstance(item, exp.ColumnDef):
            continue  # inline table constraints (PK / FK / CHECK) are not columns
        if item.kind is None:
            return None  # column without a type — treat the whole extraction as unknown
        default = _default_expression(item, dialect=dialect)
        columns.append(
            ColumnSnapshot(
                name=item.name,
                type=canonical_type(item.kind, dialect=dialect),
                nullable=not _has_not_null(item),
                default=default,
            )
        )
    return columns or None


def _has_not_null(column_def: exp.ColumnDef) -> bool:
    """True iff the column has an inline NOT NULL constraint."""
    return any(
        isinstance(constraint.kind, exp.NotNullColumnConstraint)
        for constraint in column_def.constraints or []
    )


def _default_expression(column_def: exp.ColumnDef, *, dialect: str) -> str | None:
    """Rendered default expression text, or None when the column has no DEFAULT."""
    for constraint in column_def.constraints or []:
        if isinstance(constraint.kind, exp.DefaultColumnConstraint):
            expression = constraint.kind.this
            if expression is None:
                return None
            return expression.sql(dialect=dialect)
    return None

"""Column extraction from a table's SQL body (Phase 12, ALT-1b).

The SQL script is the **single source of truth** for table columns: nothing is stored in
autodoc, so the code-first workflow (hand-edited ``CREATE TABLE``) can never drift from
the metadata. Both comparison sides go through this same extractor — the DIR side reads
the codebase files, the DB side reads the reverse-engineer's canonical DDL output.

``extract_columns`` parses the statements of the body and picks the ``CREATE``
one. A real table file is ``DROP TABLE IF EXISTS ...;`` followed by
``CREATE TABLE ...;`` and optional ``COMMENT ON`` statements — the CREATE is
not necessarily the FIRST statement, so single-statement ``parse_one`` is not
enough (it returned the Drop and made every RE-style file "columns
unavailable"). Each ``ColumnDef`` maps to a normalized
:class:`~db_project_manager.domain.delta.ColumnSnapshot`:

- ``type`` — sqlglot-rendered canonical type string. sqlglot collapses most PostgreSQL
  synonyms by itself (``integer``/``int``/``int4`` → ``INT``,
  ``character varying`` → ``VARCHAR``, ``timestamp with time zone`` → ``TIMESTAMPTZ``);
  the few it does not are handled by :data:`_TYPE_ALIASES` (BPCHAR vs CHAR,
  serial4 vs int, etc. — Phase 15.5.4).
- ``nullable`` — ``False`` iff an inline ``NOT NULL`` column constraint is present
  (table-level constraints don't affect this).
- ``default`` — rendered default expression text (e.g. ``nextval('app.t_id_seq'::REGCLASS)`),
  ``None`` when absent.

Any parse failure, non-``Create`` statement, or a ``Create`` without a column list
(``PARTITION OF`` children, ``AS SELECT``) returns ``None`` — "columns unavailable" —
which downstream classification treats fail-safe (needs-pre when the table has data).

``normalize_sql`` / ``sql_hash`` are deliberately untouched (hash-stability invariant of
Phase 12): this module does its own ``parse_one``.
"""

from __future__ import annotations

import re

import sqlglot
from sqlglot import exp

from db_project_manager.domain.delta import ColumnChangeKind, ColumnDiff, ColumnSnapshot
from db_project_manager.infrastructure.diff.normalize_sql import DEFAULT_DIALECT

#: Type synonyms sqlglot does NOT collapse on its own (base name, before modifiers).
#: Keys and values are lower-case rendered base names; modifiers are preserved as-is.
#:
#: Phase 15.5.4 (cis_zup feedback 2026-09-04): added full PG alias table to absorb the
#: remaining type-mismatch false-positives after the Phase 15.5.3 fix on DEFAULTs.
#: PG stores ``serial4``/``int4`` as the same 4-byte integer; sqlglot preserves
#: the spelling, so without this map codebase (writes ``serial4``) vs DB (after RE
#: shows ``int`` + ``nextval(...)``) would compare as type_changed (LESSONS §64).
_TYPE_ALIASES = {
    "bpchar": "char",          # RE writes udt_name bpchar; hand-written DDL says char
    # Phase 15.5.4: serial/int families
    "serial4": "int",
    "int4": "int",
    "serial8": "bigint",
    "int8": "bigint",
    "serial2": "smallint",
    "int2": "smallint",
    # Float families (sqlglot already collapses numeric → numeric, but map aliases)
    "float4": "real",
    "float8": "double precision",
    # Boolean (sqlglot keeps bool — no canonical change)
    # Character (sqlglot already collapses char_n → char_n; bpchar → char handled above)
    # Time (sqlglot already collapses timestamptz, etc.)
    # Misc
    "int4range": "int4range",
}

#: Regex pattern for ``NEXTVAL(...)`` as a default expression. Used to collapse the
#: ``serial4`` (no default in source) vs ``int + nextval(...)`` (DB round-trip) case:
#: PG itself does this transparently when a column is declared ``serial4`` — at write
#: time it adds the implicit sequence + DEFAULT NEXTVAL, at read time the catalog
#: returns ``int`` + an explicit nextval. They're functionally identical.
#: Phase 15.5.4 (cis_zup feedback 2026-09-04).
_NEXTVAL_RE = re.compile(
    r"^\s*nextval\s*\(",
    re.IGNORECASE,
)


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
        statements = sqlglot.parse(body, read=dialect)
    except Exception:  # noqa: BLE001 — sqlglot raises various error subclasses
        return None
    tree = next((s for s in statements if isinstance(s, exp.Create)), None)
    if tree is None:
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
    """True iff the column has a real inline NOT NULL constraint.

    Careful: sqlglot renders an explicit ``NULL`` marker (RE writes
    ``"col" text NULL`` for nullable columns) as
    ``NotNullColumnConstraint(allow_null=True)`` — the opposite of NOT NULL.
    Only ``allow_null`` falsy constraints count (LESSONS §44: verify the
    actual sqlglot semantics, don't assume from the class name).
    """
    for constraint in column_def.constraints or []:
        kind = constraint.kind
        if isinstance(kind, exp.NotNullColumnConstraint):
            if not kind.args.get("allow_null", False):
                return True
    return False


def _default_expression(column_def: exp.ColumnDef, *, dialect: str) -> str | None:
    """Rendered default expression text, or None when the column has no DEFAULT."""
    for constraint in column_def.constraints or []:
        if isinstance(constraint.kind, exp.DefaultColumnConstraint):
            expression = constraint.kind.this
            if expression is None:
                return None
            return expression.sql(dialect=dialect)
    return None


# ------------------------------------------------------------- column diffing


def _canonical_default(default: str | None) -> str | None:
    """Whitespace-free form of a default expression, for comparison purposes.

    Whitespace outside single-quoted literals is dropped entirely (``a + b`` ==
    ``a+b``); whitespace inside literals is preserved (``'a b'`` != ``'ab'``);
    doubled single quotes inside a literal are passed through.
    """
    if default is None:
        return None
    out: list[str] = []
    in_quote = False
    i = 0
    while i < len(default):
        ch = default[i]
        if ch == "'":
            if in_quote and i + 1 < len(default) and default[i + 1] == "'":
                out.append("''")
                i += 2
                continue
            in_quote = not in_quote
            out.append(ch)
        elif ch.isspace() and not in_quote:
            pass
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _is_serial_like_default(default: str | None) -> bool:
    """True if the default expression starts with ``NEXTVAL(...)``.

    Used by :func:`diff_columns` to absorb the case where one side declares
    ``serial4`` (and PG implicitly attaches a nextval default) while the other
    side shows ``int + DEFAULT NEXTVAL(...)`` (which is what RE reads back from
    ``pg_attrdef``). Both are functionally identical serial columns; we hide
    the difference so the column diff stays empty for serial columns.
    """
    if not default:
        return False
    return bool(_NEXTVAL_RE.match(default))


def diff_columns(
    source: list[ColumnSnapshot], target: list[ColumnSnapshot]
) -> list[ColumnDiff]:
    """Column-level diff of two extracted column lists (CD-ALT-1).

    Direction is relative to *source* (the codebase side): ADDED = in source only,
    DROPPED = in target only. A column present on both sides may yield several diffs
    (e.g. TYPE_CHANGED + NULLABILITY_CHANGED). Column renames are NOT detected — a
    rename manifests as DROPPED + ADDED (ALT-3, deliberate: guessing is unsafe).

    Phase 15.5.4: ``serialN`` columns are recognised as semantically equivalent to
    ``intN`` + ``DEFAULT nextval(...)`` (PG stores them identically inside the
    catalog; sqlglot preserves the lexical form). Without this, cis_zup's
    ``process_log_id serial4`` (codebase) vs RE-roundtripped
    ``int NOT NULL DEFAULT nextval('cis_dmt_zup.zup_process_log_process_log_id_seq'::REGCLASS)``
    falsely produced both TYPE_CHANGED + DEFAULT_CHANGED. The two effects compound:
    type canonicalisation (:data:`_TYPE_ALIASES`) collapses ``serialN`` to the
    underlying ``intN``; the explicit nextval default on one side is hidden when
    the other side has none (because at write-time PG fills it in).

    The result is sorted by column name, then by kind value, for determinism.
    """
    src_by_name = {c.name: c for c in source}
    tgt_by_name = {c.name: c for c in target}

    diffs: list[ColumnDiff] = []
    for name in sorted(set(src_by_name) | set(tgt_by_name)):
        src_col = src_by_name.get(name)
        tgt_col = tgt_by_name.get(name)
        if src_col is None:
            assert tgt_col is not None
            diffs.append(ColumnDiff(
                column=name, kind=ColumnChangeKind.DROPPED,
                source_column=None, target_column=tgt_col,
            ))
            continue
        if tgt_col is None:
            diffs.append(ColumnDiff(
                column=name, kind=ColumnChangeKind.ADDED,
                source_column=src_col, target_column=None,
            ))
            continue

        # --- Phase 15.5.4: serial / int equivalence (cis_zup feedback 2026-09-04).
        # PG stores ``serialN`` internally as ``intN + implicit DEFAULT NEXTVAL(...)``.
        # When sqlglot parses the codebase file (``serial4 NOT NULL``) it produces
        # type=serial4, default=None; after canonicalisation (Phase 15.5.4) the
        # type becomes ``int``. When RE reads the catalog back it produces
        # type=int + default=NEXTVAL(...). Both reflect the same column. We
        # therefore suppress the default-difference diff when one side has no
        # default AND the other carries a nextval-like default — they're
        # functionally identical for serial columns.
        src_default_canon = _canonical_default(src_col.default)
        tgt_default_canon = _canonical_default(tgt_col.default)
        if src_default_canon != tgt_default_canon and (
            (src_default_canon is None and _is_serial_like_default(tgt_default_canon))
            or (tgt_default_canon is None and _is_serial_like_default(src_default_canon))
        ):
            # Both sides describe the same serial column; suppress the default diff.
            src_default_canon = tgt_default_canon

        if src_col.type != tgt_col.type:
            diffs.append(ColumnDiff(
                column=name, kind=ColumnChangeKind.TYPE_CHANGED,
                source_column=src_col, target_column=tgt_col,
            ))
        if src_col.nullable != tgt_col.nullable:
            diffs.append(ColumnDiff(
                column=name, kind=ColumnChangeKind.NULLABILITY_CHANGED,
                source_column=src_col, target_column=tgt_col,
            ))
        if src_default_canon != tgt_default_canon:
            diffs.append(ColumnDiff(
                column=name, kind=ColumnChangeKind.DEFAULT_CHANGED,
                source_column=src_col, target_column=tgt_col,
            ))

    return sorted(diffs, key=lambda d: (d.column, d.kind.value))

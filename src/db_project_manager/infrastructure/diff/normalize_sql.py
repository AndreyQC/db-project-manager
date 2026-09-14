"""SQL normalization for the compare feature (Phase 9).

Two objects are considered *structurally equal* when their normalized SQL hashes
match. Normalization is done via sqlglot's AST (``parse_one(...).sql(normalize=True)``)
which makes the hash insensitive to formatting, casing of keywords, and comments.
This avoids false-positive ``changed`` flags from trivial whitespace differences.

sqlglot can fail on unusual/proprietary DDL (e.g. Greenplum-specific clauses not
yet supported by the dialect). In that case we fall back to a regex-based
normalization that collapses whitespace and strips comments — less precise, but
always returns something hashable. The fallback logs a warning so the user knows
the comparison for that object may be less reliable.

Lesson §27 (global transforms before tokenization) is honoured: sqlglot applies
its normalization to the full AST before rendering, and the regex fallback strips
comments/whitespace globally before collapsing.

Phase 15.5.3 (cis_zup feedback 2026-09-03) — additional post-AST canonicalisation
of text-literal casts: sqlglot preserves ``CAST('foo' AS TEXT)`` and ``'foo'``
as distinct tokens, so two semantically-equivalent DEFAULTs hash differently:

* ``DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'utc')``  (written in source)
* ``DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE CAST('utc' AS TEXT))``  (round-tripped)

Postgres stores both identically, but ``information_schema.columns.column_default``
or ``pg_attrdef`` may return either form depending on version. The post-AST regex
unwraps ``CAST('x' AS TEXT)`` to ``'x'`` (and vice versa for the unquoted path)
so that hash comparison recognises equality. Backed by unit tests in
``tests/unit/test_normalize_sql.py``.
"""

from __future__ import annotations

import hashlib
import re

import sqlglot
from loguru import logger
from sqlglot import exp

#: Dialect passed to sqlglot for parsing and rendering. Postgres covers both
#: PostgreSQL and Greenplum (which is Postgres-compatible at the DDL level).
DEFAULT_DIALECT = "postgres"

#: 8-hex-char digest. Same length convention as the signature hash in
#: ``domain.signature`` and the autodoc ``object_signature`` field.
_HASH_PREFIX_LEN = 8

_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT_RE = re.compile(r"--[^\n]*")
_MULTI_WS_RE = re.compile(r"\s+")

#: Post-AST canonicalisation (Phase 15.5.3):
#: ``CAST('foo' AS TEXT)`` → ``'foo'`` (PG stores both forms identically).
#: Single-quote literals only — the regex is deliberately conservative: bare
#: numerics (``CAST(1.5 AS NUMERIC)``) must NOT be collapsed (precision/format
#: is meaningful). Limited to ``'…' AS TEXT``/``'…'::text``.
_CAST_STRING_TEXT_RE = re.compile(
    r"CAST\(\s*'((?:''|[^'])*)'\s+AS\s+TEXT\s*\)",
    re.IGNORECASE,
)
#: Inverse: pg-style ``'foo'::text`` → ``'foo'`` (covers postgres shortcut).
_POSTGRES_CAST_TEXT_RE = re.compile(
    r"'((?:''|[^'])*)'\s*::\s*text\b",
    re.IGNORECASE,
)

#: Phase 16.7: ``TIMEZONE('utc', CURRENT_TIMESTAMP)`` →
#: ``CURRENT_TIMESTAMP AT TIME ZONE 'utc'``. PG's pg_attrdef round-trips the
#: AT TIME ZONE operator as the equivalent function call on older kernels
#: (Greenplum 6 = PG 9.4: ``timezone('utc'::text, now())``), while sqlglot
#: keeps the two forms distinct. After the text-cast unwrap above both sides
#: reduce to TIMEZONE('utc', CURRENT_TIMESTAMP) — this rule folds them to the
#: operator form. NOW()/CURRENT_TIMESTAMP both accepted (sqlglot normalizes
#: NOW() to CURRENT_TIMESTAMP only after a successful AST parse).
_TIMEZONE_CURRENT_TS_RE = re.compile(
    r"\bTIMEZONE\(\s*'((?:''|[^'])*)'\s*,\s*(?:CURRENT_TIMESTAMP|NOW\s*\(\s*\))\s*\)",
    re.IGNORECASE,
)

#: Phase 16.7: trailing Greenplum-only clauses of a CREATE statement —
#: ``) WITH (options) DISTRIBUTED ...;``. sqlglot cannot parse them and
#: silently degrades the whole statement to a Command node (raw text,
#: whitespace-sensitive), which made every GP table body hash-compare on
#: raw text. The tail is cut off before parsing, canonicalized separately
#: (option order/case-insensitive), and re-appended to the normalized head.
#: Matches the first ``)`` followed (optionally) by WITH/DISTRIBUTED and a
#: ``;`` — inside a column list a ``)`` is always followed by ``,``/``)``, so
#: this can only anchor at the statement's closing paren; bodies without GP
#: clauses are detected via empty groups and left untouched (PG unchanged).
_GP_TABLE_TAIL_RE = re.compile(
    r"\)\s*(WITH\s*\([^;]*?\))?\s*(DISTRIBUTED\b[^;]*?)?\s*;",
    re.IGNORECASE | re.DOTALL,
)


#: Redundant parens around the timezone expression: source-style
#: ``DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'utc')`` vs the catalog form
#: without them. Parens never change this expression's semantics — unwrap.
_TIMEZONE_PAREN_RE = re.compile(
    r"\(\s*(CURRENT_TIMESTAMP\s+AT\s+TIME\s+ZONE\s+'(?:''|[^'])*')\s*\)",
    re.IGNORECASE,
)

#: Phase 16.7: default function volatility — GP 6's pg_get_functiondef omits
#: ``VOLATILE`` (it is the default), PG 11+ emits it. Semantically identical;
#: stripped from the function HEADER only (up to the first ``$$``), never from
#: the plpgsql body where the word may appear as an identifier/string.
_DEFAULT_VOLATILE_RE = re.compile(r"\s*\bVOLATILE\b\s*", re.IGNORECASE)

#: Phase 16.7: a quoted all-lowercase identifier equals its unquoted form in
#: PG/GP (unquoted names fold to lowercase) — strip the quotes so hand-written
#: DDL (bare identifiers) hashes equal to catalog-rendered DDL (quoted).
#: Mixed-case/uppercase quoted identifiers keep their quotes (folding would
#: change the object identity). Only ``"..."`` segments — string literals use
#: single quotes and are never touched.
_LOWER_IDENT_QUOTED_RE = re.compile(r'"([a-z_][a-z0-9_]*)"')


def normalize_sql(sql: str, *, dialect: str = DEFAULT_DIALECT) -> str:
    """Normalize a DDL/DML body via sqlglot AST.

    Falls back to a regex-based normalization if sqlglot cannot parse the body.
    Never raises — returns a hashable string in all cases.

    Phase 15.5.3: after sqlglot's own normalization, applies a post-AST
    canonicalisation step (``_canonicalize_text_casts``) so that
    ``CAST('foo' AS TEXT)`` and ``'foo'`` produce identical hashes.

    Phase 16.7: Greenplum tail clauses (WITH/DISTRIBUTED) are extracted and
    canonicalized separately instead of degrading the whole statement to a
    sqlglot Command node (raw, whitespace-sensitive text).
    """
    if not sqlglot_can_parse(sql, dialect):
        return _canonicalize(_regex_normalize(sql))
    tail = _extract_gp_tail(sql)
    if tail is not None:
        head, canonical_tail = tail
        return _canonicalize(_normalize_head(head, dialect)) + "\n" + canonical_tail
    return _canonicalize(_normalize_head(sql, dialect))


def _normalize_head(sql: str, dialect: str) -> str:
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
        _canonicalize_view_ast(tree)
        return tree.sql(dialect=dialect, comments=False, normalize=True, identify=False)
    except Exception as e:  # noqa: BLE001 — sqlglot raises various error subclasses
        logger.warning(f"sqlglot не смог разобрать SQL, regex-fallback: {e}")
        return _regex_normalize(sql)


def _canonicalize_view_ast(tree: exp.Expression) -> None:
    """View-spelling equivalences (Phase 16.7):

    1. An explicit column list identical (case-insensitively) to the SELECT's
       output names is redundant — hand-written DDL states it, pg_get_viewdef
       doesn't. Dropped.
    2. A case-only alias (``src."N1_x" AS n1_x``) is pg_get_viewdef's way to
       render the case-rename a column list declared — with the list dropped
       (rule 1) the alias becomes case-only noise. Dropped as well.

    Comparison is case-insensitive because unquoted identifiers fold to
    lowercase in PG; quoted mixed-case identifiers keep their quotes, so a
    REAL rename via the list (different names, not case) still mismatches.
    """
    _drop_case_only_aliases(tree)
    if not (isinstance(tree, exp.Create) and str(tree.kind).upper() == "VIEW"):
        return
    schema, select = tree.this, tree.expression
    if (
        isinstance(schema, exp.Schema)
        and schema.expressions
        and isinstance(select, exp.Select)
        and [c.name.lower() for c in schema.expressions] == [n.lower() for n in select.named_selects]
    ):
        schema.expressions.clear()


def _drop_case_only_aliases(tree: exp.Expression) -> None:
    """``<expr> AS <same-name-different-case>`` → ``<expr>`` in select lists."""
    for alias in list(tree.find_all(exp.Alias)):
        inner = alias.this
        if isinstance(inner, (exp.Column, exp.Identifier)) and alias.alias.lower() == inner.output_name.lower():
            alias.replace(inner)


def _canonicalize(sql: str) -> str:
    """Post-normalization canonical forms shared by both paths."""
    out = _canonicalize_text_casts(sql)
    out = _TIMEZONE_CURRENT_TS_RE.sub(
        lambda m: f"CURRENT_TIMESTAMP AT TIME ZONE '{m.group(1)}'", out
    )
    out = _TIMEZONE_PAREN_RE.sub(lambda m: m.group(1), out)
    out = _strip_default_volatile(out)
    return _LOWER_IDENT_QUOTED_RE.sub(r"\1", out)


def _strip_default_volatile(sql: str) -> str:
    """Drop the default VOLATILE attribute from a CREATE FUNCTION header.

    Only applies when a ``$$`` body delimiter is present (plpgsql) — the
    attribute lives in the header before it; without ``$$`` (SQL-language
    functions with ``AS '...'``) nothing is stripped, conservatively.
    """
    head, sep, body = sql.partition("$$")
    if not sep:
        return sql
    return _DEFAULT_VOLATILE_RE.sub(" ", head, count=1) + sep + body


def _extract_gp_tail(sql: str) -> tuple[str, str] | None:
    """Split off a Greenplum WITH/DISTRIBUTED tail; None when absent."""
    match = _GP_TABLE_TAIL_RE.search(sql)
    if not match:
        return None
    with_part, dist_part = match.group(1), match.group(2)
    if not with_part and not dist_part:
        return None
    head = sql[: match.start()] + ");" + sql[match.end() :]
    return head, _canonicalize_gp_tail(with_part, dist_part)


def _canonicalize_gp_tail(with_part: str | None, dist_part: str | None) -> str:
    """Order- and case-insensitive canonical form of WITH/DISTRIBUTED clauses.

    WITH storage options are an unordered set (catalog order is not the
    declaration order), so they are sorted; identifiers/string literals keep
    their case (only unquoted tokens are lowercased). PG-side RE output has
    no such clauses, so this form only ever compares GP-vs-GP.
    """
    parts: list[str] = []
    if with_part:
        inner = re.match(r"WITH\s*\((.*)\)\s*$", with_part, re.IGNORECASE | re.DOTALL)
        options = inner.group(1) if inner else with_part
        items = sorted(
            _canonicalize_storage_option(item.strip())
            for item in _split_top_level_commas(options)
            if item.strip()
        )
        parts.append("WITH (" + ", ".join(items) + ")")
    if dist_part:
        d = _MULTI_WS_RE.sub(" ", dist_part).strip()
        d = _lower_unquoted(d)
        d = re.sub(r"\s*,\s*", ", ", d)
        d = re.sub(r"\s*\(\s*", " (", d).replace("( ", "(").replace(" )", ")")
        parts.append(d)
    return "\n".join(parts)


def _canonicalize_storage_option(option: str) -> str:
    """Canonical form of one WITH storage option.

    ``appendoptimized`` is the modern alias of the legacy catalog parameter
    name ``appendonly`` — GP 6 stores reloptions as ``appendonly=true`` while
    hand-written DDL (and newer GP docs) says ``appendoptimized=TRUE``. Same
    parameter, same semantics: fold to the catalog spelling (live-probe
    finding, Phase 16.7).
    """
    out = _lower_unquoted(option)
    return re.sub(r"\bappendoptimized\s*=", "appendonly=", out)


def _split_top_level_commas(text: str) -> list[str]:
    """Split by commas outside parens and quoted segments."""
    items: list[str] = []
    buf: list[str] = []
    depth = 0
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch in ("'", '"'):
            j = i + 1
            while j < n:
                if text[j] == ch:
                    if j + 1 < n and text[j + 1] == ch:  # doubled-quote escape
                        j += 2
                        continue
                    break
                j += 1
            buf.append(text[i : j + 1])
            i = j + 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif ch == "," and depth == 0:
            items.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    items.append("".join(buf))
    return items


def _lower_unquoted(text: str) -> str:
    """Lowercase only outside single/double-quoted segments."""
    return "".join(
        seg if seg[:1] in ("'", '"') else seg.lower()
        for seg in re.split(r"(\"(?:\"\"|[^\"])*\"|'(?:''|[^'])*')", text)
    )


def strip_gp_tail(sql: str) -> str:
    """Remove trailing Greenplum clauses (WITH/DISTRIBUTED) from the first
    CREATE statement, leaving ``);``. Input unchanged when no tail is present.

    Shared with :mod:`columns` (structural extraction, Phase 16.8): sqlglot
    degrades a GP-clause statement to a Command node with no AST, so any
    consumer that needs the real tree must cut the tail off first.
    """
    tail = _extract_gp_tail(sql)
    return tail[0] if tail is not None else sql


def sqlglot_can_parse(sql: str, dialect: str = DEFAULT_DIALECT) -> bool:
    """Quick pre-check whether sqlglot accepts the body (not empty, not whitespace-only)."""
    return bool(sql and sql.strip())


def sql_hash(sql: str) -> str:
    """Return an 8-hex-char SHA-256 prefix of the *normalized* SQL."""
    normalized = normalize_sql(sql)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return digest[:_HASH_PREFIX_LEN]


def _regex_normalize(sql: str) -> str:
    """Whitespace/comment normalization used when sqlglot can't parse.

    Global transforms first (strip comments), then collapse whitespace (lesson §27).
    """
    out = _BLOCK_COMMENT_RE.sub(" ", sql)
    out = _LINE_COMMENT_RE.sub(" ", out)
    out = _MULTI_WS_RE.sub(" ", out).strip()
    return out.lower()


def _canonicalize_text_casts(sql: str) -> str:
    """Phase 15.5.3: collapse ``CAST('x' AS TEXT)`` and ``'x'::text`` to ``'x'``.

    Both casts are semantically identical to a plain string literal in
    PostgreSQL, but pg_attrdef round-trips them through different source
    representations across versions. Without this step, the same DDL
    produces two different normalized strings and a different ``sql_hash``
    — a false-positive ``changed`` status in compare / safety gate /
    delta-plan (cis_zup feedback 2026-09-03).

    Conservative: only unwraps ``CAST(<quoted-string> AS TEXT)`` /
    ``<quoted-string>::text``. Bare numerics or other types are NOT touched
    (precision/format is meaningful — Phase 12 LESSONS §3 on random DDL).
    """
    out = _CAST_STRING_TEXT_RE.sub(lambda m: f"'{m.group(1)}'", sql)
    out = _POSTGRES_CAST_TEXT_RE.sub(lambda m: f"'{m.group(1)}'", out)
    return out

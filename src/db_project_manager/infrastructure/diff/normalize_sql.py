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
"""

from __future__ import annotations

import hashlib
import re

import sqlglot
from loguru import logger

#: Dialect passed to sqlglot for parsing and rendering. Postgres covers both
#: PostgreSQL and Greenplum (which is Postgres-compatible at the DDL level).
DEFAULT_DIALECT = "postgres"

#: 8-hex-char digest. Same length convention as the signature hash in
#: ``domain.signature`` and the autodoc ``object_signature`` field.
_HASH_PREFIX_LEN = 8

_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT_RE = re.compile(r"--[^\n]*")
_MULTI_WS_RE = re.compile(r"\s+")


def normalize_sql(sql: str, *, dialect: str = DEFAULT_DIALECT) -> str:
    """Normalize a DDL/DML body via sqlglot AST.

    Falls back to a regex-based normalization if sqlglot cannot parse the body.
    Never raises — returns a hashable string in all cases.
    """
    if not sqlglot_can_parse(sql, dialect):
        return _regex_normalize(sql)
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
        return tree.sql(dialect=dialect, comments=False, normalize=True, identify=False)
    except Exception as e:  # noqa: BLE001 — sqlglot raises various error subclasses
        logger.warning(f"sqlglot не смог разобрать SQL, regex-fallback: {e}")
        return _regex_normalize(sql)


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

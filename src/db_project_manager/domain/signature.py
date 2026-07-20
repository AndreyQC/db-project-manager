"""Canonical signature utilities for overloaded functions/procedures.

PostgreSQL allows function/procedure overloading by argument types: the same
name can have multiple distinct objects with different parameter types.

This module provides:
  * ``canonical_signature`` — normalized argument type list (mod-stripped, lowercased).
  * ``signature_hash`` — 8-hex-char SHA-256 of the canonical signature, used in
    file names and ``object_key`` suffixes to disambiguate overloads.

``argument_types`` arrives from ``pg_type.typname`` via the adapter
(``infrastructure/database/postgres/queries.py``), already normalized
(``int4``/``int8``/``bool``/``text``, not ``integer``/``bigint``/``boolean``) — so
no alias mapping is required. Only type modifiers like ``(255)`` or ``(64,0)``
need stripping, since the same logical function can be reported with or without
them by different introspection paths.
"""

from __future__ import annotations

import hashlib
import re

#: Match a parenthesized modifier anywhere in the type list. Applied to the
#: whole string BEFORE comma-splitting, so modifiers with internal commas
#: (``numeric(10,2)``) are removed in one pass without breaking the split.
#: ``varchar(255)``, ``numeric(10,2)``, ``timestamp(6)`` — all stripped, since
#: PostgreSQL distinguishes overloads by base type, not by modifier (the few
#: edge cases where modifier matters are deliberately ignored for MVP).
_MOD_RE = re.compile(r"\s*\([^)]*\)")


def canonical_signature(argument_types: str) -> str:
    """Normalize an argument type list for stable hashing.

    Examples:
        >>> canonical_signature("text, varchar, varchar, uuid")
        'text,varchar,varchar,uuid'
        >>> canonical_signature("varchar(255)")
        'varchar'
        >>> canonical_signature("numeric(10,2)")
        'numeric'
        >>> canonical_signature("")
        ''

    Args:
        argument_types: Comma-separated argument type list as produced by the
            adapter (from ``pg_type.typname``). May contain modifiers like
            ``(255)`` or ``(10,2)``.

    Returns:
        Lowercased, mod-stripped, comma-joined type list. Empty string for
        empty/whitespace input (functions without arguments).
    """
    if not argument_types or not argument_types.strip():
        return ""
    # Strip modifiers first (handles internal commas inside parens), then split.
    stripped = _MOD_RE.sub("", argument_types)
    parts = [p.strip().lower() for p in stripped.split(",")]
    parts = [p for p in parts if p]
    return ",".join(parts)


def signature_hash(argument_types: str) -> str:
    """Return 8 hex chars of SHA-256 over ``canonical_signature``.

    Empty argument_types -> empty string (no hash for functions without args,
    so they get no ``/signature/`` suffix in ``object_key`` and no SHA suffix in
    the file name).

    Examples:
        >>> len(signature_hash("int4"))
        8
        >>> signature_hash("") == ""
        True
        >>> signature_hash("varchar(255)") == signature_hash("varchar")
        True
    """
    canon = canonical_signature(argument_types)
    if not canon:
        return ""
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:8]

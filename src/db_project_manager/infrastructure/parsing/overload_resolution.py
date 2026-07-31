"""Overload resolution for function/procedure call edges (Phase 8).

When a codebase declares overloaded routines — e.g. ``sp_x(int4)`` and
``sp_x(text)`` — the edge scanner must route a *call site* to the correct
overload instead of "first wins". This module answers the narrow question:

    Given a call's argument list and the candidate overloads' argument types,
    which single overload does the call unambiguously resolve to?

Design constraints (see ``-=docs=-/phase_08/Phase_8_vision_final.md``):

* **Conservative.** Type inference is deliberately limited to *literals*. Any
  argument that is not an inferrable literal (column reference, nested call,
  arithmetic, cast, NULL, float) makes the whole call unresolved — we never
  guess. This matches the project's safety principle and LESSONS §36
  ("ambiguous = skip, not bug").
* **No I/O, pure functions.** The module is unit-testable in isolation, like
  ``domain/signature.py``. Call-site discovery in raw SQL lives here too (P8.S5)
  but the inference/comparison core has no file dependencies.
* **Canonical via ``domain/signature.py``.** Overload type tuples arrive
  canonicalized by the caller (LESSONS §27 — reuse, do not reinvent).

Argument-type literals already come canonical from ``pg_type.typname``
(``int4``/``text``/``bool``, not ``integer``/``character varying``), so the
inferred types need no further normalization to compare against overload tuples.
"""

from __future__ import annotations

import re

__all__ = [
    "infer_literal_type",
    "split_call_args",
    "infer_call_signature",
    "resolve_overload",
]


#: Single-quoted SQL string literal, allowing ``''`` as an embedded quote
#: (e.g. ``'it''s'``). Matches the whole arg from first to last quote.
_STRING_LITERAL_RE = re.compile(r"^'(?:[^']|'')*'$")

#: Double-quoted SQL *identifier* (NOT a string in PostgreSQL). We recognize it
#: only to classify it as non-inferrable — returning None, never "text".
_IDENT_LITERAL_RE = re.compile(r'^".*"$')

#: Integer literal with optional leading sign. PostgreSQL assigns an untyped
#: integer literal the type ``int4`` by default, so we infer ``int4`` exactly —
#: this matches the overload the server itself would pick in the common case.
#: If only int2/int8 overloads exist (no int4), the call is unresolved
#: (conservative; see module docstring).
_INT_LITERAL_RE = re.compile(r"^[+-]?\d+$")

#: A numeric token that is NOT a plain integer — i.e. contains a decimal point
#: or an exponent. Resolves to numeric/float8 in PG, but the exact type depends
#: on context, so we refuse to infer (returns None).
_FLOAT_LITERAL_RE = re.compile(r"^[+-]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?[._eE]")

#: SQL boolean literals (case-insensitive).
_BOOL_LITERAL_RE = re.compile(r"^(?:true|false)$", re.IGNORECASE)

#: SQL NULL literal (case-insensitive) — conveys no type information.
_NULL_LITERAL_RE = re.compile(r"^null$", re.IGNORECASE)


def infer_literal_type(arg: str) -> str | None:
    """Infer the PostgreSQL type of a single call argument, if it is a literal.

    Args:
        arg: A single argument expression, already trimmed of surrounding
            whitespace, exactly as it appeared between the call's parentheses.

    Returns:
        One of ``"text"``, ``"int4"``, ``"bool"`` — the canonical ``pg_type``
        type name — or ``None`` when the argument is not an inferrable literal.

    Inference table (Phase 8 MVP — literals only):

        ``'…'``              -> ``"text"``   (single-quoted string literal)
        ``"…"``              -> ``None``     (double-quoted *identifier*, not a string)
        ``true`` / ``false`` -> ``"bool"``
        ``123`` / ``-7``     -> ``"int4"``   (PG default for an integer literal)
        ``1.5`` / ``1e3``    -> ``None``     (numeric/float8 — context-dependent)
        ``NULL``             -> ``None``     (no type information)
        anything else        -> ``None``     (column, call, cast, arithmetic, ...)

    The function is conservative by design: it never guesses. Any uncertainty
    yields ``None``, which makes the whole call unresolved upstream.
    """
    if not arg:
        return None
    # Order matters: strings first, then the identifier special-case, then the
    # keyword literals, then numbers.
    if _STRING_LITERAL_RE.match(arg):
        return "text"
    if _IDENT_LITERAL_RE.match(arg):
        return None
    if _BOOL_LITERAL_RE.match(arg):
        return "bool"
    if _NULL_LITERAL_RE.match(arg):
        return None
    # A float-looking token must be checked BEFORE the integer regex, since a
    # purely-digit prefix would otherwise match int. We only return None for a
    # genuine float; a non-numeric token falls through to the integer check and
    # then to the final None.
    if _FLOAT_LITERAL_RE.match(arg):
        return None
    if _INT_LITERAL_RE.match(arg):
        return "int4"
    return None


def split_call_args(paren_body: str) -> list[str]:
    """Split a call's argument list (the text between the outer parens).

    Splits on commas at paren-depth 0, respecting single-quoted string
    literals (so a comma inside ``'a,b'`` does not split) and nested
    parentheses (so ``f(g(x, y), z)`` yields ``["g(x, y)", "z"]``). This avoids
    the naive ``split(",")`` that breaks on nested separators (LESSONS §27).

    Args:
        paren_body: The raw substring between the call's opening and closing
            parentheses (neither paren included). May be empty.

    Returns:
        A list of trimmed argument strings. An empty body yields ``[]`` (a
        zero-argument call). Whitespace-only arguments are kept as empty
        strings and treated as non-inferrable upstream.
    """
    args: list[str] = []
    depth = 0
    in_string = False
    current: list[str] = []
    i = 0
    n = len(paren_body)
    while i < n:
        ch = paren_body[i]
        if in_string:
            current.append(ch)
            # Handle the SQL '' escape inside a single-quoted string: two
            # adjacent quotes are an embedded quote, not a string terminator.
            if ch == "'":
                if i + 1 < n and paren_body[i + 1] == "'":
                    current.append("'")
                    i += 2
                    continue
                in_string = False
            i += 1
            continue
        if ch == "'":
            in_string = True
            current.append(ch)
        elif ch == "(":
            depth += 1
            current.append(ch)
        elif ch == ")":
            depth -= 1
            current.append(ch)
        elif ch == "," and depth == 0:
            args.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
        i += 1
    # Trailing argument (or the only argument). For an empty body current is []
    # and we must not append a phantom empty argument.
    tail = "".join(current).strip()
    if tail or args:
        args.append(tail)
    return args


def infer_call_signature(args: list[str]) -> tuple[str, ...] | None:
    """Infer the canonical argument-type tuple for a call.

    Applies :func:`infer_literal_type` to every argument. If *any* argument is
    not an inferrable literal, the whole call is unresolved and ``None`` is
    returned — partial inference is never used (a single unknown argument makes
    the entire signature unknowable).

    Args:
        args: The call's arguments as produced by :func:`split_call_args`.

    Returns:
        A tuple of canonical type names (one per argument), or ``None`` if any
        argument could not be inferred. An empty argument list yields ``()`` —
        a zero-argument call has an empty signature.
    """
    inferred: list[str] = []
    for arg in args:
        t = infer_literal_type(arg)
        if t is None:
            return None
        inferred.append(t)
    return tuple(inferred)


def resolve_overload(
    call_signature: tuple[str, ...] | None,
    overloads: list[tuple[str, tuple[str, ...]]],
) -> str | None:
    """Pick the single overload a call signature resolves to.

    Args:
        call_signature: The call's inferred canonical type tuple (from
            :func:`infer_call_signature`), or ``None`` if it could not be
            inferred. ``None`` means the call is unresolved.
        overloads: The candidate overloads as ``(object_key, canonical_types)``
            pairs. ``canonical_types`` is the canonical argument-type tuple of
            the overload (built by the caller via
            ``domain.signature.canonical_signature``).

    Returns:
        The chosen overload's ``object_key`` if exactly one overload matches
        the call signature; ``None`` if the call signature is unknown, or if
        zero or more than one overload matches (ambiguous — conservative skip).
    """
    if call_signature is None:
        return None
    matches = [key for key, types in overloads if types == call_signature]
    if len(matches) == 1:
        return matches[0]
    return None

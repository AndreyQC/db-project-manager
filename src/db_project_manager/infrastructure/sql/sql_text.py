"""Executable-SQL detection for deploy scripts.

A rendered object script may legitimately contain no executable SQL: its body
is comments only. The real case (BACKLOG P1): reverse-engineer of a database
without explicit db-level settings still emits ``settings/database
settings.sql`` — the autodoc header carries CREATE DATABASE ``properties``
(encoding / lc_collate / lc_ctype) consumed by the deploy service, but the
body is just the template's comment banner. Sending that to PostgreSQL fails
with ``can't execute an empty query``.

:func:`has_executable_sql` answers "is there anything to execute?" with a
quote-aware scan, so a ``--`` inside a string literal (``SELECT '--'``) is
never mistaken for a comment — a false "empty" verdict would silently skip
executable SQL, which is strictly worse than the bug being fixed.

Related: ``domain/deploy.py`` (canonical_normalize — deliberately keeps
comments; checksums are comment-sensitive by design, CDF-6).
"""

from __future__ import annotations

import re

#: Opening dollar-quote tag: ``$$`` or ``$tag_1$`` (PostgreSQL, any casing).
_DOLLAR_TAG_RE = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$")


def strip_sql_comments(text: str) -> str:
    """Return *text* with SQL comments removed; string literals preserved.

    Handles what a deploy script can contain:
      * ``-- ...`` line comments (to end of line, newline kept);
      * ``/* ... *``/``*`` block comments (nested markers not nested in PG);
      * ``'...'`` literals and ``"..."`` quoted identifiers, with the
        doubled-quote escape (``''`` / ``""``);
      * ``$tag$ ... $tag$`` dollar-quoted bodies (function definitions may
        contain anything, including comment-like text).

    Comments are replaced with a single space so adjacent tokens never merge.
    Unterminated literals/quotes are copied to end of input (conservative:
    better to keep content than to drop it).
    """
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if ch == "-" and nxt == "-":
            i += 2
            while i < n and text[i] != "\n":
                i += 1
            out.append(" ")
        elif ch == "/" and nxt == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i = min(i + 2, n)
            out.append(" ")
        elif ch in ("'", '"'):
            quote = ch
            j = i + 1
            while j < n:
                if text[j] == quote:
                    if j + 1 < n and text[j + 1] == quote:
                        j += 2  # escaped ('' / "")
                        continue
                    break
                j += 1
            out.append(text[i : j + 1])
            i = j + 1
        elif ch == "$":
            m = _DOLLAR_TAG_RE.match(text, i)
            if m:
                tag = m.group(0)
                end = text.find(tag, m.end())
                j = n if end == -1 else end + len(tag)
                out.append(text[i:j])
                i = j
            else:
                out.append(ch)
                i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def has_executable_sql(text: str) -> bool:
    """True if *text* contains at least one non-comment, non-whitespace char.

    Callers are expected to strip the autodoc header first
    (:func:`db_project_manager.infrastructure.sql.autodoc.strip_autodoc`);
    the YAML block is a ``/* ... */`` comment and would be ignored anyway.
    """
    return bool(strip_sql_comments(text).strip())

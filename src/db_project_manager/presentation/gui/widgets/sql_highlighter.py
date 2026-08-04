"""SQL syntax highlighter for QPlainTextEdit.

A lightweight QSyntaxHighlighter-based highlighter with no extra dependencies
(built into PySide6). This is the Phase 1 substitute for QScintilla: keywords,
types, strings, comments, numbers and identifiers are colored.

Full Scintilla-based editing (folding, etc.) is deferred to Phase 2.
"""

from __future__ import annotations

from PySide6.QtCore import QRegularExpression
from PySide6.QtGui import QColor, QFont, QSyntaxHighlighter, QTextCharFormat


def _fmt(color: str, *, bold: bool = False, italic: bool = False) -> QTextCharFormat:
    f = QTextCharFormat()
    f.setForeground(QColor(color))
    if bold:
        f.setFontWeight(QFont.Weight.Bold)
    if italic:
        f.setFontItalic(True)
    return f


#: PostgreSQL/SQL keywords.
SQL_KEYWORDS = [
    "CREATE", "OR", "REPLACE", "TABLE", "VIEW", "MATERIALIZED", "SEQUENCE", "SCHEMA",
    "FUNCTION", "PROCEDURE", "TRIGGER", "EXTENSION", "INDEX", "TYPE", "ENUM",
    "ALTER", "DROP", "ADD", "CONSTRAINT", "PRIMARY", "KEY", "FOREIGN", "REFERENCES",
    "UNIQUE", "CHECK", "DEFAULT", "NULL", "NOT", "AS", "SELECT", "FROM", "WHERE",
    "JOIN", "INNER", "LEFT", "RIGHT", "OUTER", "FULL", "ON", "GROUP", "BY", "ORDER",
    "HAVING", "LIMIT", "OFFSET", "INSERT", "INTO", "VALUES", "UPDATE", "SET", "DELETE",
    "WITH", "RETURNING", "DISTINCT", "UNION", "ALL", "AND", "OR", "IN", "EXISTS",
    "CASE", "WHEN", "THEN", "ELSE", "END", "IF", "BEGIN", "COMMIT", "ROLLBACK",
    "LANGUAGE", "SQL", "PLPGSQL", "VOLATILE", "STABLE", "IMMUTABLE", "RETURNS",
    "SETOF", "VOID", "BOOLEAN", "DO", "DECLARE", "RAISE", "NOTICE", "EXCEPTION",
    "TABLESPACE", "CACHE", "CYCLE", "INCREMENT", "MINVALUE", "MAXVALUE", "START",
    "COMMENT", "IS", "GRANT", "REVOKE", " privileges".strip(),
]

#: Common SQL/PG types.
SQL_TYPES = [
    "INTEGER", "INT", "INT2", "INT4", "INT8", "BIGINT", "SMALLINT", "SERIAL", "BIGSERIAL",
    "VARCHAR", "CHAR", "BPCHAR", "TEXT", "BOOLEAN", "BOOL", "DATE", "TIME", "TIMESTAMP",
    "TIMESTAMPTZ", "INTERVAL", "NUMERIC", "DECIMAL", "REAL", "DOUBLE", "PRECISION",
    "JSON", "JSONB", "UUID", "BYTEA", "MONEY", "INET", "CIDR", "MACADDR", "ARRAY",
]

#: Highlighting rules: (regex, format). Built once at module import.
_RULES: list[tuple[QRegularExpression, QTextCharFormat]] = []

# Keywords (word-boundary, case-insensitive).
_kw_pattern = "\\b(" + "|".join(SQL_KEYWORDS) + ")\\b"
_RULES.append((QRegularExpression(_kw_pattern, QRegularExpression.CaseInsensitiveOption), _fmt("#569CD6", bold=True)))

# Types.
_type_pattern = "\\b(" + "|".join(SQL_TYPES) + ")\\b"
_RULES.append((QRegularExpression(_type_pattern, QRegularExpression.CaseInsensitiveOption), _fmt("#4EC9B0")))

# Single-line comments (-- ...).
_RULES.append((QRegularExpression("--[^\n]*"), _fmt("#6A9955", italic=True)))

# Strings ('...').
_RULES.append((QRegularExpression("'[^'\n]*'"), _fmt("#CE9178")))

# Double-quoted identifiers ("...").
_RULES.append((QRegularExpression('"[^"\n]*"'), _fmt("#9CDCFE")))

# Numbers.
_RULES.append((QRegularExpression("\\b\\d+(\\.\\d+)?\\b"), _fmt("#B5CEA8")))

# Punctuation.
_RULES.append((QRegularExpression("[;,()]"), _fmt("#D4D4D4")))

#: Diff-marker rules applied on top of SQL when ``diff_mode=True`` (Phase 14).
#: ``^+`` / ``^-`` (added/removed line), ``^@@`` (hunk header).
_DIFF_RULES: list[tuple[QRegularExpression, QTextCharFormat]] = [
    # Added lines — green with a light background to read like a diff tool.
    (QRegularExpression("^\\+.*"), _fmt("#2EA043", bold=True)),
    # Removed lines — red.
    (QRegularExpression("^-.*"), _fmt("#F85149", bold=True)),
    # Hunk header — gray/italic.
    (QRegularExpression("^@@.*@@"), _fmt("#8B949E", italic=True)),
    # diff file headers (+++ / ---) — blue/italic.
    (QRegularExpression("^[+]{3}.*"), _fmt("#58A6FF", italic=True)),
]


class SqlHighlighter(QSyntaxHighlighter):
    """Apply SQL coloring rules to a QTextDocument.

    With ``diff_mode=True`` (Phase 14 Delta Viewer), unified-diff markers
    (``+`` / ``-`` / ``@@`` lines) are highlighted on top of the SQL rules so the
    DDL inside the diff keeps its syntax coloring while added/removed lines stand
    out.
    """

    def __init__(self, parent, *, diff_mode: bool = False) -> None:
        super().__init__(parent)
        self._diff_mode = diff_mode

    def highlightBlock(self, text: str) -> None:  # noqa: N802 (Qt override)
        for regex, fmt in _RULES:
            it = regex.globalMatch(text)
            while it.hasNext():
                match = it.next()
                self.setFormat(match.capturedStart(), match.capturedLength(), fmt)
        if self._diff_mode:
            for regex, fmt in _DIFF_RULES:
                it = regex.globalMatch(text)
                while it.hasNext():
                    match = it.next()
                    self.setFormat(match.capturedStart(), match.capturedLength(), fmt)

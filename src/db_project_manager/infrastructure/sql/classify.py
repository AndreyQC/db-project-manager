"""SQL classification for the MCP server (Phase 19, MCP-4).

Statements are parsed with sqlglot in the connection's dialect and mapped to
safety classes BEFORE execution. This is the first line of the two-layer
read-only defense (the second is the server-side read-only transaction —
see PGDatabaseAdapter.run_query); the classifier's job is precise, human-readable
rejections, not the last line of defense.

Fail-safe: a script that cannot be parsed classifies as UNKNOWN, which policy
treats exactly like DESTRUCTIVE (requires explicit confirmation).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import sqlglot
from loguru import logger
from sqlglot import exp


class StatementClass(str, Enum):
    """Safety class of a single SQL statement."""

    READ_ONLY = "read_only"
    WRITE = "write"
    DESTRUCTIVE = "destructive"
    UNKNOWN = "unknown"


#: Worst-class ranking for script-level aggregation. UNKNOWN ranks together
#: with DESTRUCTIVE (fail-safe: unparsed = untrusted).
_RANK: dict[StatementClass, int] = {
    StatementClass.READ_ONLY: 0,
    StatementClass.WRITE: 1,
    StatementClass.DESTRUCTIVE: 2,
    StatementClass.UNKNOWN: 2,
}

#: sqlglot statement key -> class. Keys verified against sqlglot 27 for the
#: postgres reader; unknown keys fall through to UNKNOWN.
_KEY_CLASS: dict[str, StatementClass] = {
    "select": StatementClass.READ_ONLY,
    "subquery": StatementClass.READ_ONLY,
    "union": StatementClass.READ_ONLY,
    "intersect": StatementClass.READ_ONLY,
    "except": StatementClass.READ_ONLY,
    "except_": StatementClass.READ_ONLY,  # older sqlglot key spelling
    "values": StatementClass.READ_ONLY,
    "describe": StatementClass.READ_ONLY,
    "insert": StatementClass.WRITE,
    "update": StatementClass.WRITE,
    "delete": StatementClass.WRITE,
    "merge": StatementClass.WRITE,
    "create": StatementClass.WRITE,
    "alter": StatementClass.WRITE,
    "grant": StatementClass.WRITE,
    "comment": StatementClass.WRITE,
    "analyze": StatementClass.WRITE,
    "copy": StatementClass.WRITE,
    "set": StatementClass.WRITE,
    "commit": StatementClass.WRITE,
    "rollback": StatementClass.WRITE,
    "truncatetable": StatementClass.DESTRUCTIVE,
    "drop": StatementClass.DESTRUCTIVE,
}

#: exp.Command verb -> class. sqlglot parks statements it does not model
#: (SHOW, EXPLAIN, REVOKE, VACUUM, CALL, DO, BEGIN ...) under Command; verbs
#: absent from both maps stay UNKNOWN (fail-safe).
_COMMAND_CLASS: dict[str, StatementClass] = {
    "SHOW": StatementClass.READ_ONLY,
    "EXPLAIN": StatementClass.READ_ONLY,
    "REVOKE": StatementClass.WRITE,
    "VACUUM": StatementClass.WRITE,
    "CALL": StatementClass.WRITE,
    "DO": StatementClass.WRITE,
    "BEGIN": StatementClass.WRITE,
    "START": StatementClass.WRITE,
    "END": StatementClass.WRITE,
}

#: db type (ConnectionConfig.type) -> sqlglot dialect. Extension point for
#: future engines (Phase 19 MCP-8): add a row together with the adapter.
DB_TYPE_DIALECT: dict[str, str] = {
    "postgres": "postgres",
    "greenplum": "postgres",
    "mssql": "tsql",
    "snowflake": "snowflake",
    "mysql": "mysql",
    "oracle": "oracle",
}

#: Functions forbidden inside read-only statements, per sqlglot dialect. A
#: read-only transaction does NOT neutralize them: dblink opens its own
#: connection, file functions bypass MVCC, nextval/setval advance sequences.
READONLY_FUNC_DENYLIST: dict[str, frozenset[str]] = {
    "postgres": frozenset(
        {
            "dblink",
            "dblink_exec",
            "pg_read_file",
            "pg_read_binary_file",
            "pg_sleep",
            "pg_sleep_for",
            "pg_sleep_until",
            "lo_import",
            "lo_export",
            "pg_terminate_backend",
            "pg_cancel_backend",
            "pg_reload_conf",
            "pg_rotate_logfile",
            "nextval",
            "setval",
        }
    ),
}


def dialect_for(db_type: str) -> str:
    """sqlglot dialect for a connection type (default: postgres)."""
    return DB_TYPE_DIALECT.get(db_type.lower(), "postgres")


@dataclass(frozen=True)
class StatementVerdict:
    """Classification of one parsed statement."""

    sql: str
    stmt_class: StatementClass
    reason: str


@dataclass(frozen=True)
class ScriptVerdict:
    """Classification of a whole script (one or more statements)."""

    statements: tuple[StatementVerdict, ...]

    @property
    def worst(self) -> StatementClass:
        """The most dangerous class present (fail-safe on empty scripts)."""
        if not self.statements:
            return StatementClass.UNKNOWN
        return max((s.stmt_class for s in self.statements), key=lambda c: _RANK[c])

    @property
    def is_read_only(self) -> bool:
        """Whether every statement is read-only (and at least one exists)."""
        return bool(self.statements) and all(
            s.stmt_class == StatementClass.READ_ONLY for s in self.statements
        )

    @property
    def requires_write_permission(self) -> bool:
        return _RANK[self.worst] >= _RANK[StatementClass.WRITE]

    @property
    def requires_destructive_confirm(self) -> bool:
        return self.worst in (StatementClass.DESTRUCTIVE, StatementClass.UNKNOWN)

    def summary(self) -> str:
        """One-line human-readable summary (Russian, surfaces to the LLM)."""
        parts = [f"{s.stmt_class.value}: {s.reason}" for s in self.statements]
        return "; ".join(parts) if parts else "пустой скрипт (нет исполняемых стейтментов)"


def _func_name(node: exp.Expression) -> str | None:
    """Last dotted segment of a function call name, lowercase."""
    try:
        name = node.name
    except Exception:  # pragma: no cover - defensive against sqlglot changes
        return None
    if not name:
        return None
    return name.split(".")[-1].lower()


def _denylisted_function(node: exp.Expression, denylist: frozenset[str]) -> str | None:
    """Return the denylisted function name used inside the statement, if any."""
    if not denylist:
        return None
    for sub in node.walk():
        if isinstance(sub, exp.Func):
            name = _func_name(sub)
            if name and name in denylist:
                return name
    return None


def _classify_one(node: exp.Expression, dialect: str, sql: str) -> StatementVerdict:
    """Classify a single parsed statement node."""
    # Unwrap parenthesized statements: ((SELECT 1)) -> SELECT 1.
    while node.key in ("subquery", "paren") and node.this is not None:
        node = node.this

    stmt_class = _KEY_CLASS.get(node.key)
    if stmt_class is None and node.key == "command":
        verb = str(getattr(node, "this", "") or "").upper()
        stmt_class = _COMMAND_CLASS.get(verb)
        if stmt_class is None:
            return StatementVerdict(sql, StatementClass.UNKNOWN, f"нераспознанная команда {verb!r}")
    if stmt_class is None:
        return StatementVerdict(sql, StatementClass.UNKNOWN, f"тип стейтмента {node.key!r} не классифицируется")

    if stmt_class == StatementClass.READ_ONLY:
        if node.find(exp.Lock):
            return StatementVerdict(sql, StatementClass.WRITE, "SELECT с блокировкой FOR UPDATE/SHARE")
        denied = _denylisted_function(node, READONLY_FUNC_DENYLIST.get(dialect, frozenset()))
        if denied:
            return StatementVerdict(
                sql, StatementClass.WRITE, f"функция {denied}() запрещена в read-only запросах"
            )

    reason = {
        StatementClass.READ_ONLY: "select/show/values/explain",
        StatementClass.WRITE: f"оператор изменения ({node.key})",
        StatementClass.DESTRUCTIVE: f"деструктивный оператор ({node.key})",
    }[stmt_class]
    return StatementVerdict(sql, stmt_class, reason)


def classify_script(sql: str, dialect: str = "postgres") -> ScriptVerdict:
    """Parse and classify every statement of a script.

    Any parse failure classifies the whole script as a single UNKNOWN
    statement (fail-safe — callers require destructive-level confirmation).
    """
    if not sql or not sql.strip():
        return ScriptVerdict(statements=())
    try:
        parsed = sqlglot.parse(sql, read=dialect)
    except Exception as e:
        reason = str(e).splitlines()[0]
        logger.info(f"classify_script: sqlglot не разобрал скрипт ({reason}) — UNKNOWN (fail-safe)")
        return ScriptVerdict(
            statements=(StatementVerdict(sql, StatementClass.UNKNOWN, f"не удалось разобрать: {reason}"),)
        )
    verdicts = tuple(
        _classify_one(node, dialect, node.sql(dialect=dialect))
        for node in parsed
        # None = empty statement; 'semicolon' = a stray separator node that
        # sqlglot emits after a trailing comment — neither is executable.
        if node is not None and node.key != "semicolon"
    )
    return ScriptVerdict(statements=verdicts)


def classify_statement(sql: str, dialect: str = "postgres") -> ScriptVerdict:
    """Convenience wrapper: same verdict type for a single-statement check."""
    return classify_script(sql, dialect=dialect)

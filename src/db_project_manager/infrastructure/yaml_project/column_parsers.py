"""Per-database-type column parsers for YAML project generation.

Each database dialect has its own DDL quirks that make it either:
- sqlglot-native: sqlglot parses the column list directly (Postgres, SQLite, etc.)
- regex-based: GP-specific DDL that sqlglot cannot parse (WITH, DISTRIBUTED BY, etc.)

The module exposes a single entry point :func:`parse_columns` that dispatches to
the right strategy based on ``db_type``. Each strategy returns a plain list of
dicts with keys: ``name``, ``type``, ``nullable``, ``default``.

Priority order (most used first):
  1. greenplum  — regex (GP DDL with WITH/DISTRIBUTED BY is not sqlglot-native)
  2. postgres   — sqlglot (fully supported)
  3. clickhouse — sqlglot + special handling for MATERIALIZED/EPHEMERAL/ALIAS columns
  4. snowflake  — sqlglot
  5. mssql      — sqlglot
  6. sqlite     — sqlglot
  7. oracle     — sqlglot
  8. <unknown>  — sqlglot with fallback to empty list (graceful degradation)
"""

from __future__ import annotations

import re

from db_project_manager.domain.yaml_project import YamlColumn
from db_project_manager.infrastructure.diff.normalize_sql import DEFAULT_DIALECT

# Supported database type literals — kept in sync with domain/yaml_project.py
_SUPPORTED_DB_TYPES = (
    "greenplum",
    "postgres",
    "clickhouse",
    "snowflake",
    "mssql",
    "sqlite",
    "oracle",
)

# ------------------------------------------------------------- public API


def parse_columns(sql_body: str, db_type: str) -> list[YamlColumn]:
    """Extract column definitions from a CREATE TABLE SQL body.

    Args:
        sql_body: raw SQL of a CREATE TABLE statement (only the DDL, not the
            autodoc header). May contain GP-specific clauses (WITH, DISTRIBUTED BY)
            that sqlglot cannot parse.
        db_type: one of ``_SUPPORTED_DB_TYPES``.

    Returns:
        List of :class:`~db_project_manager.domain.yaml_project.YamlColumn` objects,
        or an empty list when columns cannot be determined (parse error, empty body).
        An empty list is intentionally returned rather than ``None`` so that callers
        always get a list they can iterate over.
    """
    parser = _PARSERS.get(db_type, _parse_via_sqlglot)
    cols = parser(sql_body)
    # Wrap plain dicts in YamlColumn
    return [YamlColumn(name=c["name"], type=c["type"], nullable=c["nullable"], default=c["default"]) for c in cols]


# ------------------------------------------------------------- parser registry


def _register(name: str):
    """Decorator that registers a parser function under ``name`` in ``_PARSERS``."""
    def decorate(fn):
        _PARSERS[name] = fn
        return fn
    return decorate


_PARSERS: dict[str, callable] = {}


# ------------------------------------------------------------- Greenplum parser (regex-based)


@_register("greenplum")
def _parse_greenplum(sql_body: str) -> list[dict]:
    """Parse Greenplum CREATE TABLE using regex (sqlglot cannot parse GP DDL).

    Handles:
    - Comma-first column style: ``col TYPE NULL ,col2 TYPE NOT NULL``
    - Conventional style: ``col TYPE, col2 TYPE NOT NULL``
    - ``WITH (APPENDOPTIMIZED=TRUE, ...)`` clause — stripped before parsing
    - ``DISTRIBUTED BY (...)`` clause — stripped before parsing
    - ``DISTRIBUTED RANDOMLY`` — stripped before parsing
    - ``ORGANIZE`` clause — stripped before parsing
    - Trailing commas on last column — stripped
    - Quoted identifiers: ``"col"``, ``[col]``
    - Typed arguments with precision: ``NUMERIC(10,2)``
    - ``DEFAULT`` expressions with parentheses/quotes/commas
    """
    columns: list[dict] = []

    # Find the column list via parenthesis depth tracking on the FULL body.
    # We scan from the first '(' and stop at the first ')' that brings depth
    # back to 0 — this correctly handles:
    #   - Nested parens in types: NUMERIC(10,2)
    #   - DEFAULT expressions with parens: DEFAULT (CURRENT_TIMESTAMP)
    #   - LOCATION/WITH on the same line as the closing paren
    #   - LOCATION after a newline following the closing paren
    paren_start = sql_body.find("(")
    if paren_start < 0:
        return []
    depth = 0
    paren_end = -1
    for i, ch in enumerate(sql_body):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                paren_end = i
                break
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                paren_end = i
                break
    if paren_end <= paren_start:
        return []

    col_text = sql_body[paren_start + 1 : paren_end]
    for line in col_text.split("\n"):
        # Strip both leading and trailing commas — covers comma-first and
        # trailing-comma styles used across all supported databases.
        line = line.strip().lstrip(",").rstrip(",").strip()
        if not line:
            continue

        # --- DEFAULT ---
        default: str | None = None
        m_default = re.search(r"DEFAULT\s+(.+?)\s*$", line, re.IGNORECASE | re.DOTALL)
        if m_default:
            default = m_default.group(1).strip()
            line = line[: m_default.start()].strip()

        # --- NULL / NOT NULL ---
        nullable = True
        m_null = re.search(r"(NOT\s+NULL|NULL)\s*$", line, re.IGNORECASE)
        if m_null:
            nullable = m_null.group(1).upper() == "NULL"
            line = line[: m_null.start()].strip()

        # --- name + type ---
        # Pattern: ["]name["] [type]. Strip quotes/brackets from name only.
        # Handles: "col" TYPE, col TYPE, [col] TYPE, col TYPE WITHOUT TIME ZONE, etc.
        name_type_m = re.match(r"^([`\"'\[]?)(\w+)(?:\1|\])\s+(.+)$", line)
        if not name_type_m:
            continue
        raw_name = name_type_m.group(2)
        raw_type = name_type_m.group(3).strip().lower()
        if not raw_name or not raw_type:
            continue

        columns.append({
            "name": raw_name,
            "type": raw_type,
            "nullable": nullable,
            "default": default,
        })
    return columns


# ------------------------------------------------------------- Postgres parser (sqlglot-native)


@_register("postgres")
def _parse_postgres(sql_body: str) -> list[dict]:
    """Parse Postgres CREATE TABLE via sqlglot."""
    cols = _extract_via_sqlglot(sql_body, dialect="postgres")
    return [_col_snapshot_to_dict(c) for c in cols]


# ------------------------------------------------------------- ClickHouse parser


@_register("clickhouse")
def _parse_clickhouse(sql_body: str) -> list[dict]:
    """Parse ClickHouse CREATE TABLE via sqlglot, dropping MATERIALIZED/EPHEMERAL/ALIAS.

    ClickHouse sqlglot parser returns ClickHouse-specific column expressions that
    are not regular ``ColumnDef`` nodes. We fall back to regex for the common case
    since ClickHouse also supports ``col TYPE`` syntax.
    """
    cols = _extract_via_sqlglot(sql_body, dialect="clickhouse")
    result = []
    for c in cols:
        # ClickHouse adds MATERIALIZED / EPHEMERAL / ALIAS as comments or
        # computed-column expressions — skip those by checking the column kind.
        cd = getattr(c, "_col_def", None)
        if cd is not None:
            kind = getattr(cd, "kind", None)
            if kind is not None:
                kind_sql = kind.sql(dialect="clickhouse", only_sql=True).lower()
                if any(k in kind_sql for k in ("materialized", "ephemeral", "alias")):
                    continue
        result.append(_col_snapshot_to_dict(c))
    return result


# ------------------------------------------------------------- sqlglot-based parsers (snowflake, mssql, sqlite, oracle)


for _db in ("snowflake", "mssql", "sqlite", "oracle"):

    @_register(_db)
    def _parse_sqlglot(sql_body: str, _dialect: str = _db) -> list[dict]:
        cols = _extract_via_sqlglot(sql_body, dialect=_dialect)
        return [_col_snapshot_to_dict(c) for c in cols]


# ------------------------------------------------------------- unknown DB type — graceful fallback


def _parse_via_sqlglot(sql_body: str) -> list[dict]:
    """Unknown DB type: try sqlglot with the default dialect (postgres)."""
    cols = _extract_via_sqlglot(sql_body, dialect=DEFAULT_DIALECT)
    return [_col_snapshot_to_dict(c) for c in cols]


# ------------------------------------------------------------- shared sqlglot helper


def _extract_via_sqlglot(sql_body: str, dialect: str) -> list:
    """Call the shared ``extract_columns`` and return the list or empty list."""
    # Import here to avoid a circular dependency — this module is infrastructure/yaml_project,
    # columns.py lives in infrastructure/diff/.
    from db_project_manager.infrastructure.diff.columns import extract_columns

    try:
        result = extract_columns(sql_body, dialect=dialect)
        return result if result else []
    except Exception:  # noqa: BLE001
        return []


def _col_snapshot_to_dict(col) -> dict:
    """Convert a ColumnSnapshot (or similar duck) to a plain dict for our parsers."""
    return {
        "name": col.name,
        "type": col.type,
        "nullable": col.nullable,
        "default": col.default,
    }

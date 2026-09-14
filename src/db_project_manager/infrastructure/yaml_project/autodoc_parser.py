"""Parse autodoc YAML headers and SQL bodies into YamlProject domain objects.

Two entry points:
- :func:`parse_autodoc_object` — used when an autodoc header is present (most
  common case for files produced by reverse-engineer).
- :func:`parse_sql_object` — fallback when no autodoc header is present; parses
  the SQL directly with sqlglot.

For objects with autodoc:
  - ``object_schema``, ``object_type``, ``object_name``, ``object_catalog`` come
    from the header.
  - Columns: if the header carries explicit ``columns`` (populated by RE from the
    catalog), use it; otherwise fall back to :func:`extract_columns` on the SQL body.
  - ``definition`` for views/functions: the raw SQL body (after stripping autodoc).
  - GP-specific fields (``distributed_by``, ``with_options`` for tables;
    ``location``, ``format`` for external tables): always parsed from the SQL body
    via regex, because autodoc does not carry them.

For objects without autodoc (hand-written files):
  - Object identity is inferred from the ``CREATE <type> <schema>.<name>`` token
    stream via sqlglot.
  - Columns, definition, GP-specific fields: same as above.

Known limitations (documented in Phase 13):
  - ``LOCATION`` / ``FORMAT`` / ``WITH (...)`` parsing is regex-based; it covers the
    observed GP syntax but may miss edge cases.
  - ``distributed_by`` regex covers ``DISTRIBUTED BY (col1, ...)`` and
    ``DISTRIBUTED RANDOMLY``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import sqlglot
from sqlglot import exp

from db_project_manager.domain.yaml_project import (
    YamlColumn,
    YamlExternalTable,
    YamlFunction,
    YamlSchema,
    YamlTable,
    YamlView,
)
from db_project_manager.infrastructure.diff.normalize_sql import DEFAULT_DIALECT
from db_project_manager.infrastructure.yaml_project.column_parsers import parse_columns

# ------------------------------------------------------------- public API


@dataclass(frozen=True)
class ParsedSqlObject:
    """A domain object parsed from a file WITHOUT an autodoc header.

    Unlike the autodoc path (schema comes from the header), the schema here is
    derived from the qualified name in the DDL (``CREATE TABLE s.t`` → schema
    ``s``). ``schema`` is ``None`` when the DDL uses a bare name.
    """

    schema: str | None
    obj: YamlTable | YamlView | YamlFunction | YamlExternalTable


def parse_autodoc_object(
    header: dict[str, Any],
    sql_body: str,
    db_type: str,
) -> YamlTable | YamlView | YamlFunction | YamlExternalTable | None:
    """Parse one SQL file that has an autodoc header into a Yaml domain object.

    Args:
        header: parsed YAML dict from the autodoc block.
        sql_body: executable SQL after stripping the autodoc header.
        db_type: ``greenplum`` or ``postgres`` — drives which fields are expected.

    Returns:
        A Yaml domain object appropriate to ``object_type``, or ``None`` if the
        type is not supported for YAML project serialization.
    """
    obj = header.get("object", {})
    object_type = obj.get("object_type", "")
    object_schema = obj.get("object_schema") or ""
    object_name = obj.get("object_name", "")
    # object_catalog is used for identity only; not stored in YamlProject

    if object_type == "table":
        # GP external tables may be tagged as "table" in autodoc — detect by the
        # LOCATION clause. Must be a clause match (LOCATION followed by "("), NOT a
        # substring: column names like location_guid/sublocation_name contain
        # "LOCATION" and would silently misclassify regular tables as external
        # (losing NOT NULL / distributed_by / with_options — Phase 13 feedback).
        if db_type == "greenplum" and _RE_LOCATION.search(sql_body):
            return _parse_external_table_from_autodoc(obj, sql_body, object_schema, object_name)
        return _parse_table_from_autodoc(obj, sql_body, db_type, object_schema, object_name)
    elif object_type == "view":
        return _parse_view_from_autodoc(obj, sql_body, object_schema, object_name)
    elif object_type in ("function", "procedure"):
        return _parse_function_from_autodoc(obj, sql_body, object_schema, object_name)
    elif object_type == "external_table":
        if db_type != "greenplum":
            raise ValueError(
                f"external_table requires db_type=greenplum, got {db_type!r}"
            )
        return _parse_external_table_from_autodoc(obj, sql_body, object_schema, object_name)
    # Unknown / unsupported type — skip
    return None


def parse_sql_object(
    sql_body: str,
    db_type: str,
) -> ParsedSqlObject | None:
    """Parse a SQL file without an autodoc header into a Yaml domain object.

    Used as a fallback for hand-written files. Tries sqlglot first; if it returns
    a ``Command`` (GP-specific DDL that sqlglot does not support), falls back to
    regex-based extraction. The object's schema is derived from the qualified
    name in the DDL (``None`` for bare names).
    """
    try:
        tree = sqlglot.parse_one(sql_body, read=DEFAULT_DIALECT)
    except Exception:  # noqa: BLE001
        return None

    # sqlglot returns Command for GP-specific DDL it can't parse
    if isinstance(tree, exp.Command):
        return _parse_command_fallback(sql_body, db_type)

    if isinstance(tree, exp.Create):
        if isinstance(tree.this, exp.Schema):
            return _parse_table_from_tree(tree, sql_body, db_type)
        return _parse_create_from_tree(tree, sql_body, db_type)

    return None


# ------------------------------------------------------------- table


def _parse_table_from_autodoc(
    obj: dict[str, Any],
    sql_body: str,
    db_type: str,
    schema: str,
    name: str,
) -> YamlTable:
    # Columns: prefer header's columns list if present, else extract via db-specific parser
    columns: list[YamlColumn] = []
    header_cols = obj.get("columns")
    if header_cols and isinstance(header_cols, list):
        for col in header_cols:
            columns.append(YamlColumn(
                name=col.get("name", ""),
                type=col.get("type", "text"),
                nullable=col.get("nullable", True),
                default=col.get("default"),
            ))
    else:
        # DB-type-specific parser handles sqlglot-native (postgres, etc.) and
        # regex-based (greenplum, etc.) extraction internally.
        columns = parse_columns(sql_body, db_type)

    distributed_by, with_options = _parse_gp_table_options(sql_body)

    return YamlTable(
        name=name,
        columns=columns,
        distributed_by=distributed_by,
        with_options=with_options,
    )


def _parse_table_from_tree(tree: exp.Create, sql_body: str, db_type: str) -> ParsedSqlObject | None:
    """Parse a CREATE TABLE from a sqlglot tree.

    sqlglot 27 shape: ``Create(kind="TABLE", this=Schema(this=Table))`` — the
    schema comes from ``Table.db`` and the bare name from ``Table.name``
    (``Schema.name`` returns '' — the old code produced empty object names).
    """
    schema_node = tree.this
    if not isinstance(schema_node, exp.Schema):
        return None
    table_node = schema_node.this
    if not isinstance(table_node, exp.Table):
        return None

    schema = table_node.db or None
    name = table_node.name

    # Check if it's an external table
    if _is_external_table_create(tree):
        return None  # handled by _parse_external_table_from_tree

    columns: list[YamlColumn] = []
    for item in schema_node.expressions:
        if not isinstance(item, exp.ColumnDef):
            continue
        if item.kind is None:
            continue
        default = _default_expression_text(item)
        columns.append(YamlColumn(
            name=item.name,
            type=item.kind.sql(dialect=DEFAULT_DIALECT).lower(),
            nullable=not _has_not_null(item),
            default=default,
        ))

    distributed_by, with_options = _parse_gp_table_options(sql_body)
    return ParsedSqlObject(
        schema=schema,
        obj=YamlTable(
            name=name,
            columns=columns,
            distributed_by=distributed_by,
            with_options=with_options,
        ),
    )


# ------------------------------------------------------------- view


def _parse_view_from_autodoc(
    obj: dict[str, Any],
    sql_body: str,
    schema: str,
    name: str,
) -> YamlView:
    columns: list[YamlColumn] = []
    header_cols = obj.get("columns")
    if header_cols and isinstance(header_cols, list):
        for col in header_cols:
            columns.append(YamlColumn(
                name=col.get("name", ""),
                type=col.get("type", "text"),
                nullable=col.get("nullable", True),
                default=None,
            ))

    is_materialized = obj.get("object_type") == "materialized_view"
    return YamlView(
        name=name,
        columns=columns,
        definition=sql_body.strip(),
        is_materialized=is_materialized,
    )


def _parse_create_from_tree(tree: exp.Create, sql_body: str, db_type: str):
    """Dispatch a CREATE that is not a table to the right handler.

    Dispatch is by ``Create.kind`` — sqlglot 27 has NO ``exp.View`` /
    ``exp.Procedure`` nodes: views are ``Create(kind="VIEW", this=Table)``
    and functions are ``Create(kind="FUNCTION", this=UserDefinedFunction)``
    (LESSONS §44: verify AST node names on the installed version, the old
    code crashed with AttributeError on the first non-autodoc view/function).
    """
    kind = (tree.kind or "").upper()
    if kind == "VIEW":
        return _parse_view_from_tree(tree, sql_body)
    if kind in ("FUNCTION", "PROCEDURE"):
        return _parse_function_from_tree(tree, sql_body)
    return None


def _parse_view_from_tree(tree: exp.Create, sql_body: str) -> ParsedSqlObject | None:
    """Parse a CREATE VIEW / CREATE MATERIALIZED VIEW from a sqlglot tree.

    Both parse as ``Create(kind="VIEW")``; a materialized view carries a
    ``MaterializedProperty`` in ``properties``. The schema comes from
    ``Table.db`` of ``Create.this``.
    """
    view_node = tree.this
    if not isinstance(view_node, exp.Table):
        return None
    schema = view_node.db or None
    name = view_node.name

    props = tree.args.get("properties")
    is_materialized = any(
        isinstance(p, exp.MaterializedProperty)
        for p in (props.expressions if props else [])
    )

    return ParsedSqlObject(
        schema=schema,
        obj=YamlView(
            name=name,
            columns=[],  # columns extracted separately if needed
            definition=sql_body.strip(),
            is_materialized=is_materialized,
        ),
    )


# ------------------------------------------------------------- function


def _parse_function_from_autodoc(
    obj: dict[str, Any],
    sql_body: str,
    schema: str,
    name: str,
) -> YamlFunction:
    args_raw = obj.get("argument_types", "") or ""
    arguments = _parse_argument_list(args_raw)
    returns = obj.get("returns") or ""
    language = obj.get("language") or "plpgsql"
    security_definer = obj.get("security_definer", False)

    return YamlFunction(
        name=name,
        arguments=arguments,
        returns=returns,
        definition=sql_body.strip(),
        language=language,
        security_definer=security_definer,
        is_trigger=False,
    )


def _parse_function_from_tree(tree: exp.Create, sql_body: str) -> ParsedSqlObject | None:
    """Parse a CREATE FUNCTION / CREATE PROCEDURE from a sqlglot tree.

    sqlglot 27 shape: ``Create(kind="FUNCTION", this=UserDefinedFunction)`` —
    the UDF carries the name as a ``Table`` (schema in ``Table.db``) and the
    arguments as ``ColumnDef`` expressions; RETURNS / LANGUAGE / SECURITY live
    in ``properties`` (``ReturnsProperty`` / ``LanguageProperty`` /
    ``SecurityProperty``), NOT as Create args.
    """
    udf = tree.this
    if not isinstance(udf, exp.UserDefinedFunction):
        return None

    name_node = udf.this  # Table
    schema = name_node.db or None
    name = name_node.name

    arguments: list[dict[str, str]] = []
    for arg in (udf.args.get("expressions") or []):
        if isinstance(arg, exp.ColumnDef):
            arguments.append({
                "name": arg.name or "",
                "type": arg.kind.sql(dialect=DEFAULT_DIALECT).lower() if arg.kind else "any",
            })

    returns = ""
    language = "plpgsql"
    security_definer = False
    props = tree.args.get("properties")
    for prop in (props.expressions if props else []):
        if isinstance(prop, exp.ReturnsProperty) and prop.this is not None:
            returns = prop.this.sql(dialect=DEFAULT_DIALECT).lower()
        elif isinstance(prop, exp.LanguageProperty) and prop.this is not None:
            language = prop.this.name.lower()
        elif isinstance(prop, exp.SecurityProperty):
            security_definer = str(prop.this).upper() == "DEFINER"

    return ParsedSqlObject(
        schema=schema,
        obj=YamlFunction(
            name=name,
            arguments=arguments,
            returns=returns,
            definition=sql_body.strip(),
            language=language,
            security_definer=security_definer,
            is_trigger=False,
        ),
    )


# ------------------------------------------------------------- external table (Greenplum only)


def _parse_external_table_from_autodoc(
    obj: dict[str, Any],
    sql_body: str,
    schema: str,
    name: str,
) -> YamlExternalTable:
    """Parse a CREATE WRITABLE EXTERNAL TABLE from autodoc + SQL body."""
    columns: list[YamlColumn] = []
    header_cols = obj.get("columns")
    if header_cols and isinstance(header_cols, list):
        for col in header_cols:
            columns.append(YamlColumn(
                name=col.get("name", ""),
                type=col.get("type", "text"),
                nullable=col.get("nullable", True),
                default=None,
            ))
    else:
        # Extract via DB-type-specific parser (greenplum regex handles the
        # comma-first style, and forces nullable=True for external tables).
        for col in parse_columns(sql_body, "greenplum"):
            # External table columns are always nullable in GP
            columns.append(YamlColumn(
                name=col.name,
                type=col.type,
                nullable=True,  # external tables have no NOT NULL
                default=None,
            ))

    location, format_type, format_options = _parse_external_location_and_format(sql_body)
    encoding = _parse_encoding(sql_body)

    return YamlExternalTable(
        name=name,
        columns=columns,
        location=location,
        format_type=format_type,
        format_options=format_options,
        encoding=encoding,
    )


def _parse_external_table_from_tree(tree: exp.Create, sql_body: str) -> YamlExternalTable | None:
    """Parse a CREATE EXTERNAL TABLE from a sqlglot tree."""
    schema_node = tree.this
    if not isinstance(schema_node, exp.Schema):
        return None
    name = schema_node.name
    if "." in name:
        _, name = name.rsplit(".", 1)

    columns: list[YamlColumn] = []
    for item in schema_node.expressions:
        if isinstance(item, exp.ColumnDef) and item.kind:
            columns.append(YamlColumn(
                name=item.name,
                type=item.kind.sql(dialect=DEFAULT_DIALECT).lower(),
                nullable=True,  # external tables don't have NOT NULL
                default=None,
            ))

    location, format_type, format_options = _parse_external_location_and_format(sql_body)
    encoding = _parse_encoding(sql_body)

    return YamlExternalTable(
        name=name,
        columns=columns,
        location=location,
        format_type=format_type,
        format_options=format_options,
        encoding=encoding,
    )


# ------------------------------------------------------------- helpers


def _parse_command_fallback(
    sql_body: str,
    db_type: str,
) -> ParsedSqlObject | None:
    """Fallback for SQL that sqlglot cannot parse (GP-specific DDL).

    Tries to classify by keyword pattern in the raw SQL text.
    """
    upper = sql_body.upper()
    if "CREATE WRITABLE EXTERNAL TABLE" in upper or "CREATE EXTERNAL TABLE" in upper:
        if db_type != "greenplum":
            return None
        return _parse_external_table_from_raw_sql(sql_body)
    if "CREATE TABLE" in upper:
        return _parse_table_from_raw_sql(sql_body)
    return None


def _parse_table_from_raw_sql(sql_body: str) -> ParsedSqlObject | None:
    """Parse a CREATE TABLE using regex when sqlglot returned Command."""
    if not _RE_CREATE_TABLE.search(sql_body):
        return None
    columns = parse_columns(sql_body, "greenplum")
    distributed_by, with_options = _parse_gp_table_options(sql_body)
    schema, name = _extract_qualified_name(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?", sql_body
    )
    return ParsedSqlObject(
        schema=schema,
        obj=YamlTable(
            name=name,
            columns=columns,
            distributed_by=distributed_by,
            with_options=with_options,
        ),
    )


def _parse_external_table_from_raw_sql(sql_body: str) -> ParsedSqlObject | None:
    """Parse a CREATE EXTERNAL TABLE using regex when sqlglot returned Command."""
    schema, name = _extract_qualified_name(
        r"CREATE\s+(?:WRITABLE\s+)?EXTERNAL\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?",
        sql_body,
    )
    # External table columns are always nullable in GP
    columns = [
        YamlColumn(name=c.name, type=c.type, nullable=True, default=None)
        for c in parse_columns(sql_body, "greenplum")
    ]
    location, format_type, format_options = _parse_external_location_and_format(sql_body)
    encoding = _parse_encoding(sql_body)
    return ParsedSqlObject(
        schema=schema,
        obj=YamlExternalTable(
            name=name,
            columns=columns,
            location=location,
            format_type=format_type,
            format_options=format_options,
            encoding=encoding,
        ),
    )


def _extract_qualified_name(create_prefix_re: str, sql_body: str) -> tuple[str | None, str]:
    """Extract (schema, name) from the qualified object name after a CREATE prefix.

    Returns ``(None, fallback)`` when no name matches at all.
    """
    m = re.search(create_prefix_re + r'("?(?P<schema>[\w$]+)"?\s*\.\s*)?"?(?P<name>[\w$]+)"?', sql_body, re.IGNORECASE)
    if not m:
        return None, "unknown"
    return m.group("schema"), m.group("name")


_RE_CREATE_TABLE = re.compile(r"^\s*CREATE\s+TABLE", re.IGNORECASE | re.MULTILINE)


def _is_external_table_create(tree: exp.Create) -> bool:
    """Return True if this is a CREATE EXTERNAL TABLE."""
    if not isinstance(tree.this, exp.Schema):
        return False
    # Look for EXTERNAL keyword in the token stream
    # sqlglot doesn't have a dedicated node, so we check kind
    return bool(tree.kind and "EXTERNAL" in tree.kind.upper())


#: Regex for COMPRESSION / WITH (...) options in GP tables.
_RE_WITH_OPTIONS = re.compile(
    r"WITH\s*\(([^)]+)\)",
    re.IGNORECASE | re.DOTALL,
)

#: Regex for DISTRIBUTED BY clause.
_RE_DISTRIBUTED_BY = re.compile(
    r"DISTRIBUTED\s+BY\s*\(\s*([^)]+)\s*\)",
    re.IGNORECASE,
)

#: Regex for DISTRIBUTED RANDOMLY.
_RE_DISTRIBUTED_RANDOMLY = re.compile(
    r"DISTRIBUTED\s+RANDOMLY",
    re.IGNORECASE,
)

#: Regex for ENCODING clause.
_RE_ENCODING = re.compile(
    r"ENCODING\s+['\"]?([A-Z0-9_-]+)['\"]?",
    re.IGNORECASE,
)


def _parse_external_location_and_format(sql: str) -> tuple[str, str, str]:
    """Extract location, format_type, format_options from an external table SQL body.

    ``format_options`` is stored WITHOUT the outer parentheses — the SQL template
    adds them back (``FORMAT 'CUSTOM' ({{ format_options }})``). GP DDL always
    wraps the options, e.g. ``FORMAT 'CUSTOM' (FORMATTER='pxfwritable_export')``.
    """
    loc_match = _RE_LOCATION.search(sql)
    location = loc_match.group(1).strip() if loc_match else ""

    fmt_match = _RE_FORMAT.search(sql)
    if fmt_match:
        format_type = fmt_match.group(1).strip().upper()
        format_options = fmt_match.group(2).strip()
        if format_options.startswith("(") and format_options.endswith(")"):
            format_options = format_options[1:-1].strip()
    else:
        format_type = "CUSTOM"
        format_options = ""

    return location, format_type, format_options


# LOCATION ('pxf://...?QUOTE_COLUMNS=true') — URL may contain double quotes
# and other special chars. Match everything between the OUTERMOST single quotes.
_RE_LOCATION = re.compile(
    r"\bLOCATION\s*\(\s*'([^']+)'\s*\)",
    re.IGNORECASE | re.DOTALL,
)

# FORMAT 'CUSTOM' (FORMATTER=...) or FORMAT 'TEXT' etc.
# Group 1: format type. Group 2: everything until ENCODING / end of statement.
# The closing ')' of the options wrapper must NOT be a lookahead boundary —
# that truncated the options to an unbalanced "(FORMATTER='...'".
_RE_FORMAT = re.compile(
    r"FORMAT\s+'([A-Z]+)'\s*(.*?)\s*(?=ENCODING|;|$)",
    re.IGNORECASE | re.DOTALL,
)


def _parse_gp_table_options(sql: str) -> tuple[list[str], dict[str, str]]:
    """Extract ``distributed_by`` and ``with_options`` from a GP CREATE TABLE body.

    Returns:
        (distributed_by column list, with_options dict).
        Empty ``distributed_by`` means ``DISTRIBUTED RANDOMLY`` was used.
    """
    distributed_by: list[str] = []

    db_match = _RE_DISTRIBUTED_BY.search(sql)
    if db_match:
        cols_str = db_match.group(1).strip()
        distributed_by = [c.strip().strip('"') for c in cols_str.split(",") if c.strip()]
    elif _RE_DISTRIBUTED_RANDOMLY.search(sql):
        distributed_by = []

    with_options: dict[str, str] = {}
    with_match = _RE_WITH_OPTIONS.search(sql)
    if with_match:
        opts_str = with_match.group(1)
        for item in opts_str.split(","):
            item = item.strip()
            if not item:
                continue
            if "=" in item:
                key, val = item.split("=", 1)
                with_options[key.strip().lower()] = val.strip().strip("'\"")
            else:
                # Boolean flag like APPENDOPTIMIZED without =value
                with_options[item.lower()] = "true"

    return distributed_by, with_options


def _parse_argument_list(argument_types_raw: str) -> list[dict[str, str]]:
    """Parse a raw argument_types string like 'param1:text,param2:integer'."""
    if not argument_types_raw:
        return []
    result = []
    for part in argument_types_raw.split(","):
        part = part.strip()
        if ":" in part:
            name, typ = part.rsplit(":", 1)
            result.append({"name": name.strip(), "type": typ.strip()})
        else:
            result.append({"name": "", "type": part.strip()})
    return result


def _default_expression_text(col_def: exp.ColumnDef) -> str | None:
    for constraint in col_def.constraints or []:
        if isinstance(constraint.kind, exp.DefaultColumnConstraint):
            expr = constraint.kind.this
            if expr is not None:
                return expr.sql(dialect=DEFAULT_DIALECT)
    return None


def _has_not_null(col_def: exp.ColumnDef) -> bool:
    for constraint in col_def.constraints or []:
        kind = constraint.kind
        if isinstance(kind, exp.NotNullColumnConstraint):
            if not kind.args.get("allow_null", False):
                return True
    return False


def _parse_encoding(sql: str) -> str:
    m = _RE_ENCODING.search(sql)
    return m.group(1).strip().upper() if m else "UTF8"


# ------------------------------------------------------------- helper to build YamlSchema


def build_yaml_schema(
    schema_name: str,
    objects: list[YamlTable | YamlView | YamlFunction | YamlExternalTable],
) -> YamlSchema:
    """Group a list of Yaml domain objects into a YamlSchema by type."""
    tables: list[YamlTable] = []
    views: list[YamlView] = []
    functions: list[YamlFunction] = []
    external_tables: list[YamlExternalTable] = []

    for obj in objects:
        if isinstance(obj, YamlTable):
            tables.append(obj)
        elif isinstance(obj, YamlView):
            views.append(obj)
        elif isinstance(obj, YamlFunction):
            functions.append(obj)
        elif isinstance(obj, YamlExternalTable):
            external_tables.append(obj)

    return YamlSchema(
        name=schema_name,
        tables=tables,
        views=views,
        functions=functions,
        external_tables=external_tables,
    )

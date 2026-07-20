"""SQL script generator.

Renders the database structure (produced by a DatabaseAdapter) into a tree of
SQL files using Jinja2 templates. Output layout:

    <output>/<schema>/<type>s/<type> <name>.sql

e.g. <output>/bookings/tables/table aircrafts.sql

For functions/procedures the file name is kept short (``function sp_x.sql``)
unless the same name is shared by multiple overloads — in that case each file
gets a ``__<signature_hash>`` suffix built from the canonical argument types
(``function sp_x__a1b2c3d4.sql``). See ``domain.signature`` for the hash format.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader
from loguru import logger

from db_project_manager.domain.signature import signature_hash
from db_project_manager.infrastructure.sql.autodoc import ensure_header

# Types that do NOT accept (numeric_precision, numeric_scale) modifiers in DDL.
# For these types PostgreSQL's information_schema may report precision/scale
# values (e.g. int8 reports precision=64, scale=0) but the "(64, 0)" syntax
# is only valid for numeric/decimal.  Keeping the full list avoids accidental
# emission of invalid type suffixes like int8(64, 0) or timestamp(64, 0).
_NO_NUMERIC_MOD = frozenset({
    # Integers — precision/scale from information_schema describe storage
    # size, not a meaningful type modifier.
    "int2", "int4", "int8", "smallint", "integer", "bigint",
    "smallserial", "serial", "bigserial",
    # Floating-point — "precision" here is total bits, not decimal digits.
    "float4", "float8", "real", "double precision",
    # Date / time
    "date", "time", "timetz", "timestamp", "timestamptz", "interval",
    # Other scalars
    "bool", "boolean", "bytea", "money", "oid", "uuid", "xml",
    "json", "jsonb", "text", "bpchar", "char", "name",
})


def _template_helpers() -> dict[str, Any]:
    """Helpers exposed to Jinja templates to avoid inline {% if %} at line ends.

    trim_blocks/lstrip_blocks drop the newline after a block tag, so a content
    line ending in {% endif %} loses its trailing newline and lines merge.
    Moving logic into helpers keeps each line ending on plain text.
    """

    def qi(name: str) -> str:
        """Quote a SQL identifier (schema, table, column, sequence, etc.).

        Doubles embedded double-quotes per SQL standard.
        """
        return '"' + str(name).replace('"', '""') + '"'

    def qqi(*parts: str) -> str:
        """Quote a qualified name parts (e.g. schema, name) joined by dot."""
        return ".".join(qi(p) for p in parts)

    def qs(value: str) -> str:
        """Quote a SQL string literal (parameter values in ALTER ... SET).

        Doubles embedded single-quotes per SQL standard.
        """
        return "'" + str(value).replace("'", "''") + "'"

    def type_mod(col: dict[str, Any]) -> str:
        np, ns = col.get("numeric_precision"), col.get("numeric_scale")
        cml = col.get("character_maximum_length")
        udt = col.get("type", "")
        if np is not None and ns is not None and udt not in _NO_NUMERIC_MOD:
            return f"({np}, {ns})"
        if cml:
            return f"({cml})"
        return ""

    def null_mod(col: dict[str, Any]) -> str:
        return " NULL" if col.get("nullable") else " NOT NULL"

    def default_mod(col: dict[str, Any]) -> str:
        default = col.get("default")
        if default is None or default == "":
            return ""
        return f" DEFAULT {default}"

    def comma(col: dict[str, Any], loop, constraints: list) -> str:
        # Trailing comma if more columns or constraints follow.
        if not loop.last or constraints:
            return ","
        return ""

    def constraint_comma(loop) -> str:
        return "," if not loop.last else ""

    def comment_mod(obj: dict[str, Any]) -> str:
        c = obj.get("comment")
        return f" -- {c}" if c else ""

    return {
        "_qi": qi,
        "_qqi": qqi,
        "_qs": qs,
        "_type_mod": type_mod,
        "_null_mod": null_mod,
        "_default_mod": default_mod,
        "_comma": comma,
        "_constraint_comma": constraint_comma,
        "_comment_mod": comment_mod,
    }


class SQLGenerator:
    """Generate SQL files from a structure dict."""

    #: object kinds that each produce a subfolder under a schema.
    _OBJECT_KINDS = ("sequences", "tables", "views", "materialized_views", "functions", "procedures")

    def __init__(
        self,
        templates_dir: str | Path | None = None,
        *,
        autodoc: bool = True,
    ) -> None:
        if templates_dir is None:
            templates_dir = Path(__file__).resolve().parent.parent / "templates"
        self.templates_dir = Path(templates_dir)
        self.autodoc = autodoc
        self.env = Environment(
            loader=FileSystemLoader(str(self.templates_dir)),
            trim_blocks=True,
            lstrip_blocks=True,
            keep_trailing_newline=True,
        )
        self.env.globals.update(_template_helpers())

    def generate_scripts(
        self,
        structure: dict[str, Any],
        output_path: str | Path,
        *,
        object_catalog: str | None = None,
    ) -> Path:
        """Render all object scripts under ``output_path``.

        Args:
            structure: Output of DatabaseAdapter.get_database_structure().
            output_path: Root directory for the generated tree.
            object_catalog: Database name used in autodoc object_key. When None,
                autodoc headers are still emitted with this field blank.

        Returns:
            The output path (created if missing).
        """
        output_path = Path(output_path)
        output_path.mkdir(parents=True, exist_ok=True)
        logger.info(f"Генерация SQL-скриптов в: {output_path}")

        # Phase 5: global (schema-less) objects first — extensions and db settings.
        self._generate_global(structure, output_path, object_catalog)

        for schema_info in structure.get("schemas", []):
            self._generate_schema(schema_info, output_path, object_catalog)

        logger.info("Генерация SQL-скриптов завершена")
        return output_path

    # --- global (schema-less) rendering: extensions, database settings ---

    def _generate_global(
        self,
        structure: dict[str, Any],
        output_path: Path,
        object_catalog: str | None,
    ) -> None:
        """Render top-level objects that do not belong to any schema:
        extensions -> <output>/extensions/, db settings -> <output>/settings/.
        """
        for ext in structure.get("extensions") or []:
            ext_dir = output_path / "extensions"
            ext_dir.mkdir(parents=True, exist_ok=True)
            self._render_one(
                "extension.sql.j2",
                {
                    "name": ext["name"],
                    "schema": ext.get("schema"),
                    # Version is NOT pinned in DDL (Phase 5 vision Q3); the
                    # installed version is carried in the autodoc header.
                    "version": None,
                    "cascade": False,
                    "comment": ext.get("comment"),
                },
                ext_dir,
                f"extension {ext['name']}.sql",
                object_catalog=object_catalog or "",
                object_schema=None,
                object_type="extension",
                object_name=ext["name"],
                autodoc_extra={"extension_version": ext.get("version")},
            )

        database = structure.get("database") or {}
        settings = database.get("settings") or []
        properties = database.get("properties") or {}
        if not settings and not properties:
            return
        settings_dir = output_path / "settings"
        settings_dir.mkdir(parents=True, exist_ok=True)
        self._render_one(
            "database_setting.sql.j2",
            {"database_name": object_catalog or "", "settings": settings},
            settings_dir,
            "database settings.sql",
            object_catalog=object_catalog or "",
            object_schema=None,
            object_type="database_setting",
            object_name="database settings",
            autodoc_extra={"properties": properties} if properties else None,
        )

    # --- per-schema rendering ---

    def _generate_schema(self, schema_info: dict[str, Any], output_path: Path, object_catalog: str | None) -> None:
        schema_name = schema_info["name"]
        has_objects = any(schema_info.get(kind) for kind in self._OBJECT_KINDS)
        if not has_objects:
            return

        schema_dir = output_path / schema_name
        schema_dir.mkdir(parents=True, exist_ok=True)

        # Schema creation script (skip 'public' which exists by default).
        if schema_name != "public":
            self._render_one(
                "schema.sql.j2",
                {"name": schema_name, "comment": schema_info.get("comment")},
                schema_dir,
                f"schema {schema_name}.sql",
                object_catalog=object_catalog or "",
                object_schema=schema_name,
                object_type="schema",
                object_name=schema_name,
            )

        self._render_kind(schema_info, "sequences", "sequence", schema_dir, self._sequence_ctx, object_catalog)
        self._render_kind(schema_info, "tables", "table", schema_dir, self._table_ctx, object_catalog)
        self._render_kind(schema_info, "views", "view", schema_dir, self._view_ctx, object_catalog)
        self._render_kind(
            schema_info, "materialized_views", "materialized_view", schema_dir, self._matview_ctx, object_catalog
        )
        self._render_kind(schema_info, "functions", "function", schema_dir, self._function_ctx, object_catalog)
        self._render_kind(schema_info, "procedures", "procedure", schema_dir, self._procedure_ctx, object_catalog)

    def _render_kind(self, schema_info, kind, object_type, schema_dir, ctx_builder, object_catalog) -> None:
        items = schema_info.get(f"{kind}") or schema_info.get(f"{object_type}s") or []
        if not items:
            return
        kind_dir = schema_dir / kind
        kind_dir.mkdir(parents=True, exist_ok=True)
        schema_name = schema_info.get("name")

        # First pass: each ctx_builder returns (ctx, base_name, signature).
        # base_name is the short form (no signature); signature is the canonical
        # hash for functions/procedures, "" for everything else.
        prepared = [(*ctx_builder(item), item) for item in items]

        # Group by base_name to detect overloaded functions/procedures sharing
        # the same name. When a group has more than one entry, each file gets a
        # __<signature_hash> suffix to stay unique; singletons keep the short name.
        groups: dict[str, list] = {}
        for ctx, base_name, signature, item in prepared:
            groups.setdefault(base_name, []).append((ctx, signature, item))

        for base_name, group in groups.items():
            overloaded = len(group) > 1
            for ctx, signature, item in group:
                file_name = self._with_suffix(base_name, signature) if overloaded else base_name
                self._render_one(
                    f"{object_type}.sql.j2",
                    ctx,
                    kind_dir,
                    file_name,
                    object_catalog=object_catalog or "",
                    object_schema=item.get("schema", schema_name),
                    object_type=object_type,
                    object_name=item.get("name", ""),
                    object_signature=signature,
                )

    @staticmethod
    def _with_suffix(base_name: str, signature: str) -> str:
        """Insert ``__<signature>`` before the ``.sql`` extension.

        'function sp_x.sql' + 'a1b2c3d4' -> 'function sp_x__a1b2c3d4.sql'.
        When signature is empty (should not happen for an overloaded group, but
        kept defensive), the base name is returned unchanged.
        """
        if not signature:
            return base_name
        stem, dot, ext = base_name.rpartition(".")
        if not dot:  # no extension — append anyway
            return f"{base_name}__{signature}"
        return f"{stem}__{signature}{dot}{ext}"

    def _render_one(
        self,
        template_name: str,
        context: dict[str, Any],
        out_dir: Path,
        file_name: str,
        *,
        object_catalog: str,
        object_schema: str | None,
        object_type: str,
        object_name: str,
        object_signature: str = "",
        autodoc_extra: dict[str, Any] | None = None,
    ) -> None:
        template = self.env.get_template(template_name)
        script = template.render(**context)
        if self.autodoc:
            script = ensure_header(
                script,
                object_catalog=object_catalog,
                object_schema=object_schema,
                object_type=object_type,
                object_name=object_name,
                object_signature=object_signature,
                extra=autodoc_extra,
            )
        path = out_dir / file_name
        path.write_text(script, encoding="utf-8")
        logger.debug(f"Записан файл: {path}")

    # --- context builders (one per object kind) ---

    @staticmethod
    def _sequence_ctx(seq: dict[str, Any]) -> tuple[dict[str, Any], str, str]:
        ctx = {k: seq.get(k) for k in (
            "schema", "name", "comment", "owning_table", "owning_column", "data_type",
            "start", "increment", "maxvalue", "minvalue", "cache", "cycle", "last_value",
        )}
        return ctx, f"sequence {seq['name']}.sql", ""

    @staticmethod
    def _table_ctx(table: dict[str, Any]) -> tuple[dict[str, Any], str, str]:
        constraints = table.get("constraints") or []
        ctx = {
            "schema": table["schema"],
            "name": table["name"],
            "comment": table.get("comment"),
            "columns": table.get("columns") or [],
            "constraints": [c for c in constraints if c.get("type") in {"PRIMARY KEY", "UNIQUE", "CHECK"}],
            "foreign_keys": [c for c in constraints if c.get("type") == "FOREIGN KEY"],
            "indexes": table.get("indexes") or [],
        }
        return ctx, f"table {table['name']}.sql", ""

    @staticmethod
    def _view_ctx(view: dict[str, Any]) -> tuple[dict[str, Any], str, str]:
        ctx = {k: view.get(k) for k in ("schema", "name", "comment", "definition", "columns")}
        return ctx, f"view {view['name']}.sql", ""

    @staticmethod
    def _matview_ctx(mv: dict[str, Any]) -> tuple[dict[str, Any], str, str]:
        ctx = {k: mv.get(k) for k in ("schema", "name", "tablespace", "data_status", "comment", "definition", "columns")}
        return ctx, f"materialized_view {mv['name']}.sql", ""

    @staticmethod
    def _function_ctx(fn: dict[str, Any]) -> tuple[dict[str, Any], str, str]:
        signature = signature_hash(fn.get("argument_types", ""))
        ctx = {k: fn.get(k) for k in ("schema", "name", "argument_types", "definition", "comment")}
        # base_name is the short form (no signature); _render_kind adds the
        # __<hash> suffix when this name is shared by multiple overloads.
        return ctx, f"function {fn['name']}.sql", signature

    @staticmethod
    def _procedure_ctx(proc: dict[str, Any]) -> tuple[dict[str, Any], str, str]:
        signature = signature_hash(proc.get("argument_types", ""))
        ctx = {k: proc.get(k) for k in ("schema", "name", "argument_types", "definition", "comment")}
        return ctx, f"procedure {proc['name']}.sql", signature

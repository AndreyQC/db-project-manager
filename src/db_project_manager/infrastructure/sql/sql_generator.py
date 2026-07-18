"""SQL script generator.

Renders the database structure (produced by a DatabaseAdapter) into a tree of
SQL files using Jinja2 templates. Output layout:

    <output>/<schema>/<type>s/<type> <name>.sql

e.g. <output>/bookings/tables/table aircrafts.sql
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader
from loguru import logger

from db_project_manager.infrastructure.sql.autodoc import ensure_header


def _template_helpers() -> dict[str, Any]:
    """Helpers exposed to Jinja templates to avoid inline {% if %} at line ends.

    trim_blocks/lstrip_blocks drop the newline after a block tag, so a content
    line ending in {% endif %} loses its trailing newline and lines merge.
    Moving logic into helpers keeps each line ending on plain text.
    """

    def type_mod(col: dict[str, Any]) -> str:
        np, ns = col.get("numeric_precision"), col.get("numeric_scale")
        cml = col.get("character_maximum_length")
        if np is not None and ns is not None:
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

        for schema_info in structure.get("schemas", []):
            self._generate_schema(schema_info, output_path, object_catalog)

        logger.info("Генерация SQL-скриптов завершена")
        return output_path

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
        for item in items:
            ctx, file_name = ctx_builder(item)
            self._render_one(
                f"{object_type}.sql.j2",
                ctx,
                kind_dir,
                file_name,
                object_catalog=object_catalog or "",
                object_schema=item.get("schema", schema_name),
                object_type=object_type,
                object_name=item.get("name", ""),
            )

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
            )
        path = out_dir / file_name
        path.write_text(script, encoding="utf-8")
        logger.debug(f"Записан файл: {path}")

    # --- context builders (one per object kind) ---

    @staticmethod
    def _sequence_ctx(seq: dict[str, Any]) -> tuple[dict[str, Any], str]:
        ctx = {k: seq.get(k) for k in (
            "schema", "name", "comment", "owning_table", "owning_column", "data_type",
            "start", "increment", "maxvalue", "minvalue", "cache", "cycle", "last_value",
        )}
        return ctx, f"sequence {seq['name']}.sql"

    @staticmethod
    def _table_ctx(table: dict[str, Any]) -> tuple[dict[str, Any], str]:
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
        return ctx, f"table {table['name']}.sql"

    @staticmethod
    def _view_ctx(view: dict[str, Any]) -> tuple[dict[str, Any], str]:
        ctx = {k: view.get(k) for k in ("schema", "name", "comment", "definition", "columns")}
        return ctx, f"view {view['name']}.sql"

    @staticmethod
    def _matview_ctx(mv: dict[str, Any]) -> tuple[dict[str, Any], str]:
        ctx = {k: mv.get(k) for k in ("schema", "name", "tablespace", "data_status", "comment", "definition", "columns")}
        return ctx, f"materialized_view {mv['name']}.sql"

    @staticmethod
    def _function_ctx(fn: dict[str, Any]) -> tuple[dict[str, Any], str]:
        ctx = {k: fn.get(k) for k in ("schema", "name", "argument_types", "definition", "comment")}
        return ctx, f"function {fn['name']}({fn.get('argument_types', '')}).sql"

    @staticmethod
    def _procedure_ctx(proc: dict[str, Any]) -> tuple[dict[str, Any], str]:
        ctx = {k: proc.get(k) for k in ("schema", "name", "argument_types", "definition", "comment")}
        return ctx, f"procedure {proc['name']}({proc.get('argument_types', '')}).sql"

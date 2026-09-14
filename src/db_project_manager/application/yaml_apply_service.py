"""Apply a YamlProject to a target directory: generate SQL files + manifest + graph (Phase 13, S4).

Consumes a ``YamlProject`` (parsed from a YAML file) and produces:
- SQL files using the existing Jinja2 templates (table, view, function, external_table)
- ``dbpm.manifest.json`` with the target database type
- ``.dbm_graph/`` via :class:`BuildGraphService`

GP → Postgres transformations:
- ``external_table`` objects are skipped (Postgres has no writable external tables)
- ``distributed_by`` and ``with_options`` are stripped from tables (they are GP-only)

Postgres → Greenplum:
- Not validated: Greenplum accepts all Postgres DDL, GP-specific fields simply
  won't be present in the YAML.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from jinja2 import Environment, FileSystemLoader
from loguru import logger

from db_project_manager.application.graph_service import BuildGraphService
from db_project_manager.domain.yaml_project import (
    YamlExternalTable,
    YamlFunction,
    YamlProject,
    YamlSchema,
    YamlTable,
    YamlView,
)
from db_project_manager.infrastructure.config.codebase_manifest import (
    CodebaseManifest,
    write_manifest,
)
from db_project_manager.infrastructure.deploy.canonical_ddl import (
    DEFAULT_SERVICE_SCHEMA,
    seed_deploy_files,
)


def _template_helpers() -> dict:
    """Shared helpers for YAML-based template rendering (same as sql_generator)."""

    def qi(name: str) -> str:
        return '"' + str(name).replace('"', '""') + '"'

    def qqi(*parts: str) -> str:
        return ".".join(qi(p) for p in parts)

    def null_mod(col: dict) -> str:
        return " NULL" if col.get("nullable", True) else " NOT NULL"

    def default_mod(col: dict) -> str:
        # YAML columns carry the DEFAULT expression as raw text (parsed from the
        # source DDL) — re-emit it verbatim. Dropping it here silently lost
        # DEFAULTs on the apply → generate roundtrip (244 columns on cis_zup).
        d = col.get("default")
        return f" DEFAULT {d}" if d else ""

    def comma(col: dict, loop, constraints: list | None = None) -> str:
        return "," if not loop.last else ""

    def type_mod(col: dict) -> str:
        # YAML tables don't carry numeric_precision/scale or character_maximum_length,
        # so this always returns empty string — sufficient for the apply use case.
        return ""

    def comment_mod(obj: dict) -> str:
        c = obj.get("comment")
        return f" -- {c}" if c else ""

    return {
        "_qi": qi,
        "_qqi": qqi,
        "_null_mod": null_mod,
        "_default_mod": default_mod,
        "_type_mod": type_mod,
        "_comma": comma,
        "_comment_mod": comment_mod,
        "_constraint_comma": lambda loop: "",
    }


@dataclass(frozen=True)
class YamlApplyResult:
    """Result of a successful YAML apply operation."""

    output_dir: Path
    schemas_count: int
    objects_count: int
    skipped_external_tables: int


class YamlApplyError(Exception):
    """Raised when a YamlProject cannot be applied."""


class YamlApplyService:
    """Generate a codebase from a YamlProject and write it to disk."""

    def __init__(self, service_schema: str = DEFAULT_SERVICE_SCHEMA) -> None:
        templates_dir = Path(__file__).resolve().parent.parent / "infrastructure" / "templates"
        self._env = Environment(
            loader=FileSystemLoader(str(templates_dir)),
            trim_blocks=True,
            lstrip_blocks=True,
            keep_trailing_newline=True,
        )
        self._env.globals.update(_template_helpers())
        self._graph_service = BuildGraphService()
        self._service_schema = service_schema

    def run(
        self,
        project: YamlProject,
        output_dir: Path,
        target_db_type: str,
    ) -> YamlApplyResult:
        """Apply a YamlProject to a target directory.

        Args:
            project: parsed YamlProject.
            output_dir: directory to write the generated codebase.
            target_db_type: ``greenplum`` or ``postgres``.

        Returns:
            Result with counts and output path.

        Raises:
            YamlApplyError: if validation fails or a file cannot be written.
        """
        self._validate(project, target_db_type)

        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        schemas_count = 0
        objects_count = 0
        skipped_external = 0

        for schema in project.schemas:
            schemas_count += 1
            schema_dir = output_dir / schema.name
            schema_dir.mkdir(parents=True, exist_ok=True)

            # Schema SQL
            objects_count += self._write_schema_sql(schema, schema_dir, project, target_db_type)

            # Tables
            for table in schema.tables:
                objects_count += self._write_table_sql(table, schema, output_dir, project, target_db_type)

            # Views
            for view in schema.views:
                objects_count += self._write_view_sql(view, schema, output_dir, project)

            # Functions
            for function in schema.functions:
                objects_count += self._write_function_sql(function, schema, output_dir, project)

            # External tables (GP only)
            if target_db_type == "greenplum":
                for ext in schema.external_tables:
                    objects_count += self._write_external_table_sql(ext, schema, output_dir, project)
            else:
                skipped_external += len(schema.external_tables)
                if skipped_external:
                    logger.warning(
                        f"Skipped {skipped_external} external table(s) for target_db_type=postgres"
                    )

        # Seed the service schema (__deploy: schema + 3 bookkeeping tables) so
        # the produced codebase is deployable as-is — deploy validate's
        # _validate_deploy_presence requires these files (feedback 01.09).
        # overwrite=False: a re-apply into an existing codebase never clobbers.
        seeded = seed_deploy_files(
            output_dir / self._service_schema,
            self._service_schema,
            project.database,
        )
        if seeded:
            logger.info(
                f"Служебная схема {self._service_schema}: создано {len(seeded)} "
                f"canonical-файлов (schema + 3 таблицы)"
            )

        # Write manifest.
        # source_version: if YAML has no calver, seed from generated_at date so the
        # written manifest always satisfies Phase 10's format_version=2 requirement.
        # format_version=2 is always used — Phase 10 requires it for all new writes.
        _src = project.source_version
        if not _src:
            date_part = project.generated_at[:10]  # "YYYY-MM-DD"
            _src = date_part.replace("-", ".") + ".01"  # "YYYY.MM.DD.01"
        manifest = CodebaseManifest(
            db_type=target_db_type,
            database=project.database,
            generated_at=project.generated_at,
            source_version=_src,
            format_version=2,
        )
        write_manifest(manifest, output_dir)

        # Build graph
        self._graph_service.build_and_store(output_dir)

        logger.info(
            f"YAML apply done: schemas={schemas_count}, objects={objects_count}, "
            f"output={output_dir}"
        )

        return YamlApplyResult(
            output_dir=output_dir,
            schemas_count=schemas_count,
            objects_count=objects_count,
            skipped_external_tables=skipped_external,
        )

    # ----------------------------------------------------------------- validation

    def _validate(self, project: YamlProject, target_db_type: str) -> None:
        """Validate that target_db_type is compatible with the project."""
        if project.db_type == "postgres" and target_db_type == "greenplum":
            pass  # Postgres DDL is valid on Greenplum
        elif project.db_type == "greenplum" and target_db_type == "postgres":
            # Check if there are external tables — those can't be applied to Postgres
            for schema in project.schemas:
                if schema.external_tables:
                    raise YamlApplyError(
                        f"Cannot apply greenplum project to postgres: "
                        f"schema {schema.name!r} has external tables "
                        f"({len(schema.external_tables)} object(s)) which Postgres does not support. "
                        f"Use --target-db-type=greenplum instead."
                    )
        elif project.db_type == "greenplum" and target_db_type == "greenplum":
            pass
        elif project.db_type == "postgres" and target_db_type == "postgres":
            pass
        else:
            raise YamlApplyError(
                f"Unknown db_type combination: source={project.db_type!r}, target={target_db_type!r}"
            )

    # ----------------------------------------------------------------- SQL writers

    def _write_schema_sql(
        self,
        schema: YamlSchema,
        schema_dir: Path,
        project: YamlProject,
        target_db_type: str,
    ) -> int:
        """Write the schema SQL file. Returns 1 (one object written)."""
        obj_key = f"pg_database/{project.database}/schema/{schema.name}/type/schema/name/{schema.name}"
        autodoc = _build_autodoc_header(
            project=project,
            object_schema=schema.name,
            object_type="schema",
            object_name=schema.name,
            object_key=obj_key,
        )
        sql = f"""\
/*====================================================================================
[<[autodoc-yaml]]\n{autodoc}[[autodoc-yaml]>]
=====================================================================================*/

CREATE SCHEMA IF NOT EXISTS {self._qi(schema.name)};
"""
        path = schema_dir / f"schema {schema.name}.sql"
        path.write_text(sql, encoding="utf-8")
        return 1

    def _write_table_sql(
        self,
        table: YamlTable,
        schema: YamlSchema,
        output_dir: Path,
        project: YamlProject,
        target_db_type: str,
    ) -> int:
        """Write a table SQL file. Returns 1."""
        obj_key = (
            f"pg_database/{project.database}/schema/{schema.name}/"
            f"type/table/name/{table.name}"
        )
        autodoc = _build_autodoc_header(
            project=project,
            object_schema=schema.name,
            object_type="table",
            object_name=table.name,
            object_key=obj_key,
        )

        # Build columns context (dict form for Jinja)
        columns = [
            {
                "name": c.name,
                "type": c.type,
                "nullable": c.nullable,
                "default": c.default,
                "comment": None,
            }
            for c in table.columns
        ]

        # Template context
        ctx = {
            "schema": schema.name,
            "name": table.name,
            "columns": columns,
            "constraints": [],
            "foreign_keys": [],
            "comment": None,
        }

        # GP-specific clauses: WITH (...) and DISTRIBUTED BY (...) are SEPARATE
        # clauses in GP DDL. WITH must render even when distributed_by is empty
        # (DISTRIBUTED RANDOMLY) — gating both on distributed_by silently dropped
        # with_options for 123 tables on cis_zup.
        if target_db_type == "greenplum":
            gp_parts = []
            if table.with_options:
                opts = ", ".join(
                    f"{k}={v}" for k, v in table.with_options.items()
                )
                gp_parts.append(f"WITH ({opts})")
            if table.distributed_by:
                cols_str = ", ".join(self._qi(c) for c in table.distributed_by)
                gp_parts.append(f"DISTRIBUTED BY ({cols_str})")
            ctx["gp_options"] = "\n".join(gp_parts)
        else:
            ctx["gp_options"] = ""

        template = self._env.get_template("table.sql.j2")
        sql_body = template.render(**ctx)

        # Inject gp_options into the CREATE TABLE statement
        if ctx["gp_options"]:
            sql_body = _inject_gp_options(sql_body, ctx["gp_options"])

        sql = f"""\
/*====================================================================================
[<[autodoc-yaml]]\n{autodoc}[[autodoc-yaml]>]
=====================================================================================*/

{sql_body}
"""
        tables_dir = output_dir / schema.name / "tables"
        tables_dir.mkdir(parents=True, exist_ok=True)
        path = tables_dir / f"table {table.name}.sql"
        path.write_text(sql, encoding="utf-8")
        return 1

    def _write_view_sql(
        self,
        view: YamlView,
        schema: YamlSchema,
        output_dir: Path,
        project: YamlProject,
    ) -> int:
        """Write a view SQL file. Returns 1.

        ``definition`` is the FULL statement body (``CREATE [OR REPLACE] VIEW
        ... AS ...``) — it is emitted VERBATIM. The old code wrapped it in the
        view template's own ``CREATE OR REPLACE VIEW ... AS``, producing an
        invalid double-CREATE (feedback 01.09); the template is only used by
        the RE path, where ``definition`` is a bare SELECT.
        """
        obj_key = (
            f"pg_database/{project.database}/schema/{schema.name}/"
            f"type/{'materialized_view' if view.is_materialized else 'view'}/name/{view.name}"
        )
        autodoc = _build_autodoc_header(
            project=project,
            object_schema=schema.name,
            object_type="view" if not view.is_materialized else "materialized_view",
            object_name=view.name,
            object_key=obj_key,
        )
        sql = f"""\
/*====================================================================================
[<[autodoc-yaml]]\n{autodoc}[[autodoc-yaml]>]
=====================================================================================*/

{view.definition.strip()}
"""
        # Layout follows the RE convention (<schema>/<kind>/<object_type> <name>.sql):
        # views -> views/, materialized views -> materialized_views/ (separate kind dir).
        if view.is_materialized:
            views_dir = output_dir / schema.name / "materialized_views"
            file_name = f"materialized_view {view.name}.sql"
        else:
            views_dir = output_dir / schema.name / "views"
            file_name = f"view {view.name}.sql"
        views_dir.mkdir(parents=True, exist_ok=True)
        path = views_dir / file_name
        path.write_text(sql, encoding="utf-8")
        return 1

    def _write_function_sql(
        self,
        function: YamlFunction,
        schema: YamlSchema,
        output_dir: Path,
        project: YamlProject,
    ) -> int:
        """Write a function/procedure SQL file. Returns 1.

        ``definition`` is the full statement body — emitted VERBATIM (no
        wrapping comment line, no appended ``;``) so that a generate roundtrip
        reproduces the definition exactly.
        """
        obj_type = "function"
        obj_key = (
            f"pg_database/{project.database}/schema/{schema.name}/"
            f"type/{obj_type}/name/{function.name}"
        )
        autodoc = _build_autodoc_header(
            project=project,
            object_schema=schema.name,
            object_type=obj_type,
            object_name=function.name,
            object_key=obj_key,
        )
        sql = f"""\
/*====================================================================================
[<[autodoc-yaml]]\n{autodoc}[[autodoc-yaml]>]
=====================================================================================*/

{function.definition.strip()}
"""
        funcs_dir = output_dir / schema.name / "functions"
        funcs_dir.mkdir(parents=True, exist_ok=True)
        path = funcs_dir / f"function {function.name}.sql"
        path.write_text(sql, encoding="utf-8")
        return 1

    def _write_external_table_sql(
        self,
        ext: YamlExternalTable,
        schema: YamlSchema,
        output_dir: Path,
        project: YamlProject,
    ) -> int:
        """Write an external table SQL file. Returns 1."""
        obj_key = (
            f"pg_database/{project.database}/schema/{schema.name}/"
            f"type/external_table/name/{ext.name}"
        )
        autodoc = _build_autodoc_header(
            project=project,
            object_schema=schema.name,
            object_type="external_table",
            object_name=ext.name,
            object_key=obj_key,
        )
        template = self._env.get_template("external_table.sql.j2")
        columns = [
            {
                "name": c.name,
                "type": c.type,
                "nullable": c.nullable,
            }
            for c in ext.columns
        ]
        ctx = {
            "schema": schema.name,
            "name": ext.name,
            "columns": columns,
            "location": ext.location,
            "format_type": ext.format_type,
            "format_options": ext.format_options,
            "encoding": ext.encoding,
        }
        sql_body = template.render(**ctx)
        sql = f"""\
/*====================================================================================
[<[autodoc-yaml]]\n{autodoc}[[autodoc-yaml]>]
=====================================================================================*/

{sql_body}
"""
        ext_dir = output_dir / schema.name / "external_tables"
        ext_dir.mkdir(parents=True, exist_ok=True)
        path = ext_dir / f"external_table {ext.name}.sql"
        path.write_text(sql, encoding="utf-8")
        return 1

    # ----------------------------------------------------------------- helpers

    def _qi(self, name: str) -> str:
        return '"' + str(name).replace('"', '""') + '"'


# --------------------------------------------------------------------- private helpers


def _build_autodoc_header(
    *,
    project: YamlProject,
    object_schema: str,
    object_type: str,
    object_name: str,
    object_key: str,
) -> str:
    """Build a YAML autodoc block string for prepending to a SQL file."""
    import yaml

    meta = {
        "object": {
            "object_catalog": project.database,
            "object_schema": object_schema,
            "object_type": object_type,
            "object_name": object_name,
            "object_key": object_key,
        },
        "project": {"build": True},
    }
    return yaml.safe_dump(meta, allow_unicode=True, sort_keys=False)


def _inject_gp_options(sql: str, gp_options: str) -> str:
    """Insert GP clauses (WITH (...) / DISTRIBUTED BY (...)) after the closing
    parenthesis of the column list.

    The template ends the CREATE TABLE statement with ``);``. The clauses go
    BETWEEN the closing ``)`` and the ``;``::

        CREATE TABLE t (
            ...
        )
        WITH (...)
        DISTRIBUTED BY (...);

    The old implementation replaced the FIRST ``);`` — which consumed the
    column-list closing paren and produced invalid SQL (``...NOT NULL
    WITH (... DISTRIBUTED BY (x)));``), plus the roundtrip parser then read
    ``WITH`` as a column name.
    """
    idx = sql.rfind(");")
    if idx < 0:
        return sql
    return sql[:idx] + ")\n" + gp_options + ";" + sql[idx + 2 :]

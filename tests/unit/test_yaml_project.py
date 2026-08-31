"""Unit tests for the YAML project feature (Phase 13).

Covers:
- Roundtrip: YamlProject → serialize → parse → equal
- GP-only fields ignored when target=postgres (error case)
- PG + external_table → error
- generate_yaml_project on a synthetic directory
- apply → files created + manifest + graph built
- SQL template rendering: external table
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from db_project_manager.application.yaml_apply_service import (
    YamlApplyError,
    YamlApplyService,
)
from db_project_manager.domain.yaml_project import (
    YamlColumn,
    YamlExternalTable,
    YamlFunction,
    YamlProject,
    YamlSchema,
    YamlTable,
    YamlView,
)
from db_project_manager.infrastructure.yaml_project import (
    generate_yaml_project,
    parse_yaml_project,
    serialize_yaml_project,
)
from db_project_manager.infrastructure.yaml_project.autodoc_parser import (
    parse_autodoc_object,
)


class TestSerializerRoundtrip:
    """Test serialize → parse produces equal objects."""

    def test_minimal_project_roundtrip(self):
        project = YamlProject(
            db_type="postgres",
            database="testdb",
            generated_at="2026-08-27T00:00:00+00:00",
            source_version="",
            schemas=[],
        )
        yaml_text = serialize_yaml_project(project)
        parsed = parse_yaml_project(yaml_text)
        assert parsed.db_type == "postgres"
        assert parsed.database == "testdb"
        assert parsed.schemas == []

    def test_full_project_roundtrip(self):
        project = YamlProject(
            db_type="greenplum",
            database="mydb",
            generated_at="2026-08-27T00:00:00+00:00",
            source_version="2026.08.27.01",
            schemas=[
                YamlSchema(
                    name="public",
                    tables=[
                        YamlTable(
                            name="users",
                            columns=[
                                YamlColumn(name="id", type="int4", nullable=False, default=None),
                                YamlColumn(name="name", type="text", nullable=True, default="'Anonymous'"),
                            ],
                            distributed_by=["id"],
                            with_options={"appendoptimized": "true", "orientation": "column"},
                        ),
                    ],
                    views=[
                        YamlView(
                            name="v_active_users",
                            columns=[],
                            definition="SELECT id, name FROM public.users WHERE active = true",
                            is_materialized=False,
                        ),
                    ],
                    functions=[
                        YamlFunction(
                            name="get_count",
                            arguments=[{"name": "p_limit", "type": "int4"}],
                            returns="int4",
                            definition="SELECT COUNT(*) FROM users LIMIT p_limit",
                            language="plpgsql",
                            security_definer=False,
                            is_trigger=False,
                        ),
                    ],
                    external_tables=[
                        YamlExternalTable(
                            name="ext_sales",
                            columns=[
                                YamlColumn(name="amount", type="numeric", nullable=True, default=None),
                            ],
                            location="pxf://sales?PROFILE=JDBC&SERVER=gp",
                            format_type="CUSTOM",
                            format_options="FORMATTER='pxfwritable_export'",
                            encoding="UTF8",
                        ),
                    ],
                ),
            ],
        )
        yaml_text = serialize_yaml_project(project)
        parsed = parse_yaml_project(yaml_text)

        assert parsed.db_type == "greenplum"
        assert parsed.database == "mydb"
        assert parsed.source_version == "2026.08.27.01"
        assert len(parsed.schemas) == 1

        s = parsed.schemas[0]
        assert s.name == "public"
        assert len(s.tables) == 1
        assert s.tables[0].name == "users"
        assert s.tables[0].distributed_by == ["id"]
        assert s.tables[0].with_options == {"appendoptimized": "true", "orientation": "column"}

        assert len(s.views) == 1
        assert s.views[0].name == "v_active_users"

        assert len(s.functions) == 1
        assert s.functions[0].name == "get_count"
        assert s.functions[0].arguments[0]["name"] == "p_limit"

        assert len(s.external_tables) == 1
        assert s.external_tables[0].name == "ext_sales"
        assert "pxf" in s.external_tables[0].location


class TestDescriptiveKeys:
    """Serialized entities use descriptive name keys (schema_name, table_name, ...)."""

    def _full_project(self) -> YamlProject:
        return YamlProject(
            db_type="greenplum",
            database="mydb",
            generated_at="2026-08-27T00:00:00+00:00",
            schemas=[
                YamlSchema(
                    name="public",
                    tables=[
                        YamlTable(
                            name="users",
                            columns=[YamlColumn(name="id", type="int4", nullable=False)],
                        ),
                    ],
                    views=[YamlView(name="v_users", definition="SELECT 1")],
                    functions=[YamlFunction(name="get_count", definition="SELECT 1")],
                    external_tables=[
                        YamlExternalTable(
                            name="ext_sales",
                            location="pxf://sales",
                            columns=[YamlColumn(name="amount", type="numeric")],
                        ),
                    ],
                ),
            ],
        )

    def test_serialized_yaml_uses_descriptive_keys(self):
        yaml_text = serialize_yaml_project(self._full_project())
        # Raw dict (roundtrip through yaml.safe_load) — robust to quoting styles (LESSONS §28)
        raw = yaml.safe_load(yaml_text)
        schema = raw["schemas"][0]
        assert schema["schema_name"] == "public"
        assert schema["tables"][0]["table_name"] == "users"
        assert schema["tables"][0]["columns"][0]["column_name"] == "id"
        assert schema["views"][0]["view_name"] == "v_users"
        assert schema["functions"][0]["function_name"] == "get_count"
        assert schema["external_tables"][0]["external_table_name"] == "ext_sales"
        assert schema["external_tables"][0]["columns"][0]["column_name"] == "amount"

    def test_legacy_name_keys_still_parse(self):
        """YAML written before the descriptive-key change (bare `name`) still parses."""
        legacy_yaml = """
db_type: postgres
database: mydb
generated_at: "2026-08-27T00:00:00+00:00"
schemas:
  - name: public
    tables:
      - name: users
        columns:
          - name: id
            type: int4
            nullable: false
    views:
      - name: v_users
        definition: SELECT 1
    functions:
      - name: get_count
        definition: SELECT 1
"""
        parsed = parse_yaml_project(legacy_yaml)
        s = parsed.schemas[0]
        assert s.name == "public"
        assert s.tables[0].name == "users"
        assert s.tables[0].columns[0].name == "id"
        assert s.tables[0].columns[0].nullable is False
        assert s.views[0].name == "v_users"
        assert s.functions[0].name == "get_count"


class TestGpParsingRegressions:
    """Regressions from real-codebase feedback 2026-08-31 (GP cis_zup)."""

    def test_location_column_name_does_not_make_table_external(self):
        """A column named location_guid must not turn a regular GP table into
        an external one (the old substring check "LOCATION" in sql matched it)."""
        sql_body = """DROP TABLE IF EXISTS s.t CASCADE;

CREATE TABLE s.t (
    position_code TEXT NULL
    ,location_guid TEXT NULL
    ,source_system_key TEXT NOT NULL
)
WITH (APPENDOPTIMIZED = TRUE, ORIENTATION = COLUMN)
DISTRIBUTED BY (source_system_key);
"""
        header = {"object": {
            "object_type": "table", "object_schema": "s", "object_name": "t",
        }}
        obj = parse_autodoc_object(header, sql_body, "greenplum")
        assert isinstance(obj, YamlTable)
        assert obj.name == "t"
        # NOT NULL survived (external-table path would force nullable=True everywhere)
        cols = {c.name: c for c in obj.columns}
        assert cols["source_system_key"].nullable is False
        assert cols["location_guid"].nullable is True
        # GP options extracted instead of being lost
        assert obj.distributed_by == ["source_system_key"]
        assert obj.with_options == {"appendoptimized": "TRUE", "orientation": "COLUMN"}

    def test_real_external_table_still_detected_and_format_options_full(self):
        sql_body = """CREATE WRITABLE EXTERNAL TABLE s.ext_t (
    "x" NUMERIC (38, 0),
    "y" TEXT
)
LOCATION ('pxf://tbl?PROFILE=JDBC&SERVER=s')
FORMAT 'CUSTOM' (FORMATTER='pxfwritable_export')
ENCODING 'UTF8';
"""
        header = {"object": {
            "object_type": "table", "object_schema": "s", "object_name": "ext_t",
        }}
        obj = parse_autodoc_object(header, sql_body, "greenplum")
        assert isinstance(obj, YamlExternalTable)
        assert obj.location == "pxf://tbl?PROFILE=JDBC&SERVER=s"
        # Outer parens stripped (the SQL template adds them back); NOT the old
        # truncated "(FORMATTER='pxfwritable_export'" without a closing paren.
        assert obj.format_options == "FORMATTER='pxfwritable_export'"
        assert obj.encoding == "UTF8"
        # Type with GP-style spaces normalized
        assert obj.columns[0].type == "numeric(38,0)"

    def test_column_type_normalization(self):
        assert YamlColumn(name="a", type="NUMERIC (38, 0)").type == "numeric(38,0)"
        assert YamlColumn(name="a", type="varchar(50)").type == "varchar(50)"
        # Multi-word types keep their inner spaces
        assert YamlColumn(name="a", type="double precision").type == "double precision"

    def test_serializer_puts_name_key_first(self):
        """The entity name key must be the FIRST key of its block, so a long
        YAML file can be scanned by name (feedback: alphabetical sorting buried
        schema_name under hundreds of column lines)."""
        project = YamlProject(
            db_type="postgres",
            database="d",
            generated_at="2026-08-31T00:00:00+00:00",
            schemas=[YamlSchema(
                name="s",
                tables=[YamlTable(name="t", columns=[YamlColumn(name="c", type="text")])],
            )],
        )
        raw = yaml.safe_load(serialize_yaml_project(project))
        assert list(raw["schemas"][0])[0] == "schema_name"
        assert list(raw["schemas"][0]["tables"][0])[0] == "table_name"
        assert list(raw["schemas"][0]["tables"][0]["columns"][0])[0] == "column_name"


class TestGenerateYamlProject:
    """Test generate_yaml_project on a synthetic directory."""

    def test_generates_yaml_project_from_files(self, tmp_path: Path):
        # Create a minimal SQL file structure
        schema_dir = tmp_path / "testdb" / "public"
        tables_dir = schema_dir / "tables" / "table"
        tables_dir.mkdir(parents=True)

        sql = """\
/*====================================================================================
[<[autodoc-yaml]]
object:
  object_catalog: testdb
  object_key: pg_database/testdb/schema/public/type/table/name/users
  object_name: users
  object_schema: public
  object_type: table
project:
  build: true
[[autodoc-yaml]>]
=====================================================================================*/

CREATE TABLE public.users (
    id INT NOT NULL,
    name TEXT NULL
);
"""
        (tables_dir / "table users.sql").write_text(sql, encoding="utf-8")

        project = generate_yaml_project(tmp_path / "testdb", db_type="postgres")
        assert project.db_type == "postgres"
        assert project.database == "testdb"
        assert len(project.schemas) == 1
        assert project.schemas[0].name == "public"
        assert len(project.schemas[0].tables) == 1
        assert project.schemas[0].tables[0].name == "users"


class TestYamlApplyValidation:
    """Test YamlApplyService validation logic."""

    def test_external_table_greenplum_to_postgres_raises(self):
        project = YamlProject(
            db_type="greenplum",
            database="testdb",
            generated_at="2026-08-27T00:00:00+00:00",
            source_version="",
            schemas=[
                YamlSchema(
                    name="public",
                    external_tables=[
                        YamlExternalTable(
                            name="ext_data",
                            columns=[YamlColumn(name="x", type="text", nullable=True)],
                            location="pxf://data",
                            format_type="CUSTOM",
                            format_options="",
                            encoding="UTF8",
                        ),
                    ],
                ),
            ],
        )
        service = YamlApplyService()
        with pytest.raises(YamlApplyError, match="external tables.*Postgres does not support"):
            service.run(project, Path("/tmp/out"), "postgres")

    def test_external_table_greenplum_to_greenplum_succeeds(self, tmp_path: Path):
        project = YamlProject(
            db_type="greenplum",
            database="testdb",
            generated_at="2026-08-27T00:00:00+00:00",
            source_version="",
            schemas=[
                YamlSchema(
                    name="public",
                    external_tables=[
                        YamlExternalTable(
                            name="ext_data",
                            columns=[YamlColumn(name="x", type="text", nullable=True)],
                            location="pxf://data?PROFILE=JDBC&SERVER=s1",
                            format_type="CUSTOM",
                            format_options="FORMATTER='pxfwritable_export'",
                            encoding="UTF8",
                        ),
                    ],
                ),
            ],
        )
        service = YamlApplyService()
        result = service.run(project, tmp_path, "greenplum")
        assert result.schemas_count == 1
        # objects = schema + external_table
        assert result.objects_count == 2
        assert result.skipped_external_tables == 0

        # External table SQL file was written
        ext_file = tmp_path / "public" / "external_tables" / "ext" / "ext ext_data.sql"
        assert ext_file.exists()
        content = ext_file.read_text(encoding="utf-8")
        assert "CREATE WRITABLE EXTERNAL TABLE" in content
        assert "pxf://data" in content

    def test_postgres_project_to_postgres_succeeds(self, tmp_path: Path):
        project = YamlProject(
            db_type="postgres",
            database="testdb",
            generated_at="2026-08-27T00:00:00+00:00",
            source_version="",
            schemas=[
                YamlSchema(
                    name="public",
                    tables=[
                        YamlTable(
                            name="users",
                            columns=[
                                YamlColumn(name="id", type="int4", nullable=False, default=None),
                            ],
                        ),
                    ],
                ),
            ],
        )
        service = YamlApplyService()
        result = service.run(project, tmp_path, "postgres")
        assert result.schemas_count == 1
        # objects = schema.sql + 1 table
        assert result.objects_count == 2
        assert result.skipped_external_tables == 0

        # Check manifest
        manifest_path = tmp_path / "dbpm.manifest.json"
        assert manifest_path.exists()
        manifest = json.loads(manifest_path.read_text())
        assert manifest["db_type"] == "postgres"
        assert manifest["database"] == "testdb"
        assert manifest["format_version"] == 2  # source_version seeded from generated_at

        # Check graph
        assert (tmp_path / ".dbm_graph").exists()

    def test_skips_external_tables_for_postgres_target(self, tmp_path: Path):
        """When a GP project has external tables but target=postgres, they are skipped."""
        project = YamlProject(
            db_type="greenplum",
            database="testdb",
            generated_at="2026-08-27T00:00:00+00:00",
            source_version="",
            schemas=[
                YamlSchema(
                    name="public",
                    tables=[
                        YamlTable(
                            name="users",
                            columns=[YamlColumn(name="id", type="int4", nullable=False)],
                        ),
                    ],
                    external_tables=[
                        YamlExternalTable(
                            name="ext_data",
                            columns=[YamlColumn(name="x", type="text", nullable=True)],
                            location="pxf://data",
                            format_type="CUSTOM",
                            format_options="",
                            encoding="UTF8",
                        ),
                    ],
                ),
            ],
        )
        service = YamlApplyService()
        result = service.run(project, tmp_path, "greenplum")
        # objects = schema.sql + 1 table + 1 external_table
        assert result.objects_count == 3


class TestExternalTableTemplate:
    """Test that external table SQL is rendered correctly."""

    def test_external_table_render(self, tmp_path: Path):
        project = YamlProject(
            db_type="greenplum",
            database="testdb",
            generated_at="2026-08-27T00:00:00+00:00",
            source_version="",
            schemas=[
                YamlSchema(
                    name="ch",
                    external_tables=[
                        YamlExternalTable(
                            name="ext_sales",
                            columns=[
                                YamlColumn(name="period", type="date", nullable=False),
                                YamlColumn(name="amount", type="numeric", nullable=True),
                            ],
                            location="pxf://sales?PROFILE=JDBC&SERVER=ch",
                            format_type="CUSTOM",
                            format_options="FORMATTER='pxfwritable_export'",
                            encoding="UTF8",
                        ),
                    ],
                ),
            ],
        )
        service = YamlApplyService()
        service.run(project, tmp_path, "greenplum")

        ext_file = tmp_path / "ch" / "external_tables" / "ext" / "ext ext_sales.sql"
        content = ext_file.read_text(encoding="utf-8")

        assert "CREATE WRITABLE EXTERNAL TABLE" in content
        assert '"ch"."ext_sales"' in content
        assert "period" in content
        assert "amount" in content
        assert "pxf://sales" in content
        assert "FORMAT 'CUSTOM'" in content
        assert "ENCODING 'UTF8'" in content
        assert "DROP EXTERNAL TABLE IF EXISTS" in content
        assert "[autodoc-yaml]" in content  # autodoc header written

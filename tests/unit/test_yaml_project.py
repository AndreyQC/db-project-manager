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
    parse_sql_object,
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


class TestParseSqlObject:
    """Files WITHOUT an autodoc header go through the sqlglot tree path.

    Regression (feedback 01.09): the dispatcher referenced non-existent
    sqlglot 27 nodes (exp.View / exp.Materialized / exp.Procedure) and any
    hand-written view/function crashed with AttributeError. The object schema
    is derived from the qualified DDL name (was: always "public").
    """

    def test_table_without_autodoc(self):
        parsed = parse_sql_object(
            'CREATE TABLE s.t1 (id int NOT NULL, name text)', "postgres"
        )
        assert parsed is not None and parsed.schema == "s"
        obj = parsed.obj
        assert isinstance(obj, YamlTable)
        assert obj.name == "t1"
        cols = {c.name: c for c in obj.columns}
        assert cols["id"].nullable is False
        assert cols["name"].type == "text"

    def test_bare_table_without_autodoc_has_no_schema(self):
        parsed = parse_sql_object("CREATE TABLE t1 (id int)", "postgres")
        assert parsed is not None
        assert parsed.schema is None
        assert parsed.obj.name == "t1"

    def test_view_without_autodoc(self):
        parsed = parse_sql_object(
            "CREATE VIEW s.v1 AS SELECT id FROM s.t1", "postgres"
        )
        assert parsed is not None and parsed.schema == "s"
        obj = parsed.obj
        assert isinstance(obj, YamlView)
        assert obj.name == "v1"
        assert obj.is_materialized is False
        assert "SELECT id" in obj.definition

    def test_materialized_view_without_autodoc(self):
        parsed = parse_sql_object(
            "CREATE MATERIALIZED VIEW s.mv1 AS SELECT 1", "postgres"
        )
        assert parsed is not None
        obj = parsed.obj
        assert isinstance(obj, YamlView)
        assert obj.name == "mv1"
        assert obj.is_materialized is True

    def test_function_without_autodoc(self):
        sql = (
            "CREATE FUNCTION s.f1(x int, y text) RETURNS int "
            "LANGUAGE plpgsql SECURITY DEFINER AS $$ SELECT x $$"
        )
        parsed = parse_sql_object(sql, "postgres")
        assert parsed is not None and parsed.schema == "s"
        obj = parsed.obj
        assert isinstance(obj, YamlFunction)
        assert obj.name == "f1"
        assert obj.arguments == [{"name": "x", "type": "int"}, {"name": "y", "type": "text"}]
        assert obj.returns == "int"
        assert obj.language == "plpgsql"
        assert obj.security_definer is True
        assert "SELECT x" in obj.definition

    def test_gp_table_via_regex_fallback_keeps_schema(self):
        """GP DDL (WITH/DISTRIBUTED BY) is not sqlglot-native -> Command ->
        regex fallback; the schema must survive there too."""
        sql = """CREATE TABLE mysch.t1 (
    id INT NOT NULL
    ,location_guid TEXT NULL
)
WITH (APPENDOPTIMIZED = TRUE)
DISTRIBUTED BY (id);
"""
        parsed = parse_sql_object(sql, "greenplum")
        assert parsed is not None
        assert parsed.schema == "mysch"
        assert isinstance(parsed.obj, YamlTable)
        assert parsed.obj.name == "t1"
        assert parsed.obj.distributed_by == ["id"]
        assert parsed.obj.with_options == {"appendoptimized": "TRUE"}

    def test_gp_external_table_via_regex_fallback_keeps_schema(self):
        sql = """CREATE WRITABLE EXTERNAL TABLE mysch.ext1 (
    x TEXT
)
LOCATION ('pxf://t?PROFILE=JDBC')
FORMAT 'CUSTOM' (FORMATTER='pxfwritable_export')
ENCODING 'UTF8';
"""
        parsed = parse_sql_object(sql, "greenplum")
        assert parsed is not None
        assert parsed.schema == "mysch"
        assert isinstance(parsed.obj, YamlExternalTable)
        assert parsed.obj.name == "ext1"
        assert parsed.obj.location == "pxf://t?PROFILE=JDBC"


class TestNoAutodocReporting:
    """Files without an autodoc header: warned by default, error in strict mode."""

    def _make_dir(self, tmp_path: Path) -> Path:
        """Codebase with one autodoc table (schema s1) and one hand-written
        function without autodoc (schema s2 via qualified DDL name)."""
        src = tmp_path / "src"
        (src / "s1" / "tables").mkdir(parents=True)
        (src / "s1" / "tables" / "table users.sql").write_text(
            """/*====================================================================================
[<[autodoc-yaml]]
object:
  object_catalog: testdb
  object_key: pg_database/testdb/schema/s1/type/table/name/users
  object_name: users
  object_schema: s1
  object_type: table
project:
  build: true
[[autodoc-yaml]>]
=====================================================================================*/

CREATE TABLE s1.users (
    id INT NOT NULL
);
""",
            encoding="utf-8",
        )
        (src / "s2" / "functions").mkdir(parents=True)
        (src / "s2" / "functions" / "function get_count.sql").write_text(
            "CREATE FUNCTION s2.get_count() RETURNS int LANGUAGE plpgsql AS $$ SELECT 1 $$",
            encoding="utf-8",
        )
        return src

    def test_schema_from_qualified_ddl_name(self, tmp_path: Path):
        project = generate_yaml_project(self._make_dir(tmp_path), db_type="postgres")
        schemas = {s.name: s for s in project.schemas}
        assert "s1" in schemas and "s2" in schemas  # was: s2 forced into "public"
        assert schemas["s2"].functions[0].name == "get_count"
        assert project.database == "testdb"  # db name still from autodoc header

    def test_warn_lists_no_autodoc_files(self, tmp_path: Path):
        from loguru import logger

        messages: list[str] = []
        sink_id = logger.add(messages.append, level="WARNING")
        try:
            generate_yaml_project(self._make_dir(tmp_path), db_type="postgres")
        finally:
            logger.remove(sink_id)
        assert any("function get_count.sql" in m for m in messages)
        assert any("1 файл(ов) без autodoc-заголовка" in m for m in messages)

    def test_require_autodoc_raises_with_file_list(self, tmp_path: Path):
        from db_project_manager.infrastructure.yaml_project import YamlGeneratorError

        with pytest.raises(YamlGeneratorError, match="function get_count.sql"):
            generate_yaml_project(self._make_dir(tmp_path), db_type="postgres", require_autodoc=True)

    def test_require_autodoc_passes_when_all_have_headers(self, tmp_path: Path):
        src = self._make_dir(tmp_path)
        (src / "s2" / "functions" / "function get_count.sql").unlink()
        project = generate_yaml_project(src, db_type="postgres", require_autodoc=True)
        assert [s.name for s in project.schemas] == ["s1"]

    def test_broken_autodoc_header_reported_separately(self, tmp_path: Path):
        """Marker present but invalid YAML (unquoted value with ': ') — identity
        is salvaged line-wise, a fresh autodoc is regenerated in-memory; the file
        on disk is NOT touched by default."""
        from loguru import logger

        src = self._make_dir(tmp_path)
        (src / "s1" / "tables" / "table bad.sql").write_text(
            """/*====================================================================================
[<[autodoc-yaml]]
object:
  object_catalog: testdb
  object_schema: s1
  object_type: table
  object_name: bad
notes: converted bad.sql: DROP -> DROP, LOCATION removed
[[autodoc-yaml]>]
=====================================================================================*/

CREATE TABLE s1.bad (id INT);
""",
            encoding="utf-8",
        )
        messages: list[str] = []
        sink_id = logger.add(messages.append, level="WARNING")
        try:
            project = generate_yaml_project(src, db_type="postgres")
        finally:
            logger.remove(sink_id)

        assert any("невалидным YAML в autodoc" in m for m in messages)
        assert any("файлы НЕ изменялись" in m for m in messages)
        # Identity salvaged: object landed in the header-declared schema
        s1 = {s.name: s for s in project.schemas}["s1"]
        assert "bad" in {t.name for t in s1.tables}
        # Read-only by default
        content = (src / "s1" / "tables" / "table bad.sql").read_text(encoding="utf-8")
        assert "notes: converted bad.sql:" in content  # untouched

    def test_fix_broken_autodoc_rewrites_header_in_place(self, tmp_path: Path):
        """--fix-broken-autodoc: the broken block is replaced by a fresh valid
        autodoc (identity preserved, invalid extras dropped); SQL body kept."""
        src = self._make_dir(tmp_path)
        bad = src / "s1" / "tables" / "table bad.sql"
        bad.write_text(
            """/*====================================================================================
[<[autodoc-yaml]]
object:
  object_catalog: testdb
  object_schema: s1
  object_type: table
  object_name: bad
notes: converted bad.sql: DROP -> DROP
[[autodoc-yaml]>]
=====================================================================================*/

CREATE TABLE s1.bad (id INT);
""",
            encoding="utf-8",
        )

        project = generate_yaml_project(src, db_type="postgres", fix_broken_autodoc=True)

        # Object still parsed correctly
        s1 = {s.name: s for s in project.schemas}["s1"]
        assert "bad" in {t.name for t in s1.tables}
        # Header on disk is now VALID YAML and carries the salvaged identity
        from db_project_manager.infrastructure.sql.autodoc import extract_header

        fixed = bad.read_text(encoding="utf-8")
        header = extract_header(fixed)
        assert header is not None
        assert header["object"]["object_schema"] == "s1"
        assert header["object"]["object_name"] == "bad"
        assert header["object"]["object_type"] == "table"
        # SQL body preserved
        assert "CREATE TABLE s1.bad (id INT);" in fixed
        # The invalid section is gone
        assert "notes: converted" not in fixed

    def test_require_autodoc_fails_on_broken_header(self, tmp_path: Path):
        from db_project_manager.infrastructure.yaml_project import YamlGeneratorError

        src = self._make_dir(tmp_path)
        (src / "s1" / "tables" / "table bad.sql").write_text(
            """[<[autodoc-yaml]]
object:
  object_type: table
  object_name: bad
broken: value: with colon
[[autodoc-yaml]>]

CREATE TABLE s1.bad (id INT);
""",
            encoding="utf-8",
        )
        with pytest.raises(YamlGeneratorError, match="невалидный YAML"):
            generate_yaml_project(src, db_type="postgres", require_autodoc=True)


class TestGenerateYamlProject:
    """Test generate_yaml_project on a synthetic directory."""

    def test_generates_yaml_project_from_files(self, tmp_path: Path):
        # Create a minimal SQL file structure (RE layout: <schema>/<kind>/<type> <name>.sql)
        schema_dir = tmp_path / "testdb" / "public"
        tables_dir = schema_dir / "tables"
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

        # External table SQL file was written (RE layout: <schema>/<kind>/<object_type> <name>.sql)
        ext_file = tmp_path / "public" / "external_tables" / "external_table ext_data.sql"
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


class TestApplyLayout:
    """yaml apply must reproduce the RE directory layout exactly:
    ``<schema>/<kind>/<object_type> <name>.sql`` — no nested per-type
    subdirectory, no double prefixes (feedback 31.08: external_tables/ext/
    "ext ext_x.sql" instead of external_tables/"external_table ext_x.sql").
    """

    def test_all_object_types_use_re_layout(self, tmp_path: Path):
        project = YamlProject(
            db_type="greenplum",
            database="d",
            generated_at="2026-08-31T00:00:00+00:00",
            schemas=[YamlSchema(
                name="s",
                tables=[YamlTable(name="t1", columns=[YamlColumn(name="c", type="text")])],
                views=[
                    YamlView(name="v1", definition="SELECT 1"),
                    YamlView(name="mv1", definition="SELECT 2", is_materialized=True),
                ],
                functions=[YamlFunction(name="f1", definition="SELECT 1")],
                external_tables=[YamlExternalTable(
                    name="ext1", location="pxf://t", columns=[YamlColumn(name="c", type="text")],
                )],
            )],
        )
        YamlApplyService().run(project, tmp_path, "greenplum")

        expected = [
            "s/schema s.sql",
            "s/tables/table t1.sql",
            "s/views/view v1.sql",
            "s/materialized_views/materialized_view mv1.sql",
            "s/functions/function f1.sql",
            "s/external_tables/external_table ext1.sql",
        ]
        for rel in expected:
            assert (tmp_path / rel).exists(), f"missing {rel}"
        # No nested per-type subdirectories, no double "ext ext_" prefixes
        assert not (tmp_path / "s" / "tables" / "table").exists()
        assert not (tmp_path / "s" / "external_tables" / "ext").exists()

    def test_gp_options_and_defaults_rendered(self, tmp_path: Path):
        """WITH/DISTRIBUTED BY are separate clauses after the closing paren;
        column DEFAULTs survive the apply (were silently dropped)."""
        project = YamlProject(
            db_type="greenplum",
            database="d",
            generated_at="2026-08-31T00:00:00+00:00",
            schemas=[YamlSchema(
                name="s",
                tables=[
                    YamlTable(
                        name="t1",
                        columns=[
                            YamlColumn(name="id", type="int4", nullable=False),
                            YamlColumn(
                                name="ts", type="timestamp", nullable=False,
                                default="(CURRENT_TIMESTAMP AT TIME ZONE 'utc')",
                            ),
                        ],
                        distributed_by=["id"],
                        with_options={"appendoptimized": "TRUE", "orientation": "COLUMN"},
                    ),
                    # WITH options without distributed_by (DISTRIBUTED RANDOMLY)
                    # must still render the WITH clause.
                    YamlTable(
                        name="t2",
                        columns=[YamlColumn(name="c", type="text", nullable=True)],
                        with_options={"orientation": "COLUMN"},
                    ),
                ],
            )],
        )
        YamlApplyService().run(project, tmp_path, "greenplum")

        t1 = (tmp_path / "s" / "tables" / "table t1.sql").read_text(encoding="utf-8")
        assert ")\nWITH (appendoptimized=TRUE, orientation=COLUMN)\nDISTRIBUTED BY (\"id\");" in t1
        assert " DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'utc')" in t1

        t2 = (tmp_path / "s" / "tables" / "table t2.sql").read_text(encoding="utf-8")
        assert ")\nWITH (orientation=COLUMN);" in t2

    def test_view_and_function_written_verbatim(self, tmp_path: Path):
        """definition is a FULL statement body — it must be emitted verbatim.
        Regression (feedback 01.09): the view template wrapped it in its own
        CREATE OR REPLACE VIEW ... AS -> invalid double CREATE."""
        view_def = (
            "CREATE OR REPLACE VIEW s.v1 (\n    a\n    ,b\n) AS\nSELECT 1;"
        )
        func_def = "CREATE FUNCTION s.f1() RETURNS int LANGUAGE plpgsql AS $$ SELECT 1 $$;"
        project = YamlProject(
            db_type="postgres",
            database="d",
            generated_at="2026-09-01T00:00:00+00:00",
            schemas=[YamlSchema(
                name="s",
                views=[YamlView(name="v1", definition=view_def)],
                functions=[YamlFunction(name="f1", definition=func_def)],
            )],
        )
        YamlApplyService().run(project, tmp_path, "postgres")

        v = (tmp_path / "s" / "views" / "view v1.sql").read_text(encoding="utf-8")
        assert v.count("CREATE OR REPLACE VIEW") == 1
        assert view_def in v  # verbatim, including the column list

        f = (tmp_path / "s" / "functions" / "function f1.sql").read_text(encoding="utf-8")
        assert f.count("CREATE FUNCTION") == 1
        assert func_def in f
        assert not f.rstrip().endswith(";;")  # no duplicated semicolon

        # Roundtrip: generate on the applied dir reproduces the definitions exactly
        rt = generate_yaml_project(tmp_path, db_type="postgres")
        s = {x.name: x for x in rt.schemas}["s"]
        assert s.views[0].definition.strip() == view_def
        assert s.functions[0].definition.strip() == func_def


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

        ext_file = tmp_path / "ch" / "external_tables" / "external_table ext_sales.sql"
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

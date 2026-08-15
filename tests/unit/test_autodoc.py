"""Tests for db_project_manager.infrastructure.sql.autodoc."""

from __future__ import annotations

from db_project_manager.infrastructure.sql.autodoc import (
    MARKER_CLOSE,
    MARKER_OPEN,
    build_metadata,
    ensure_header,
    extract_header,
    render_header,
    update_header,
)


def test_build_metadata_has_object_key() -> None:
    meta = build_metadata(
        object_catalog="mydb", object_schema="bookings", object_type="table", object_name="aircrafts"
    )
    assert meta["object"]["object_catalog"] == "mydb"
    assert meta["object"]["object_schema"] == "bookings"
    assert meta["object"]["object_type"] == "table"
    assert meta["object"]["object_name"] == "aircrafts"
    assert meta["object"]["object_key"] == "pg_database/mydb/schema/bookings/type/table/name/aircrafts"
    assert meta["project"]["build"] is True


def test_object_key_omits_schema_when_none() -> None:
    meta = build_metadata(object_catalog="mydb", object_schema=None, object_type="schema", object_name="public")
    assert meta["object"]["object_key"] == "pg_database/mydb/type/schema/name/public"


def test_render_header_contains_markers_and_yaml() -> None:
    meta = build_metadata(object_catalog="db", object_schema="s", object_type="view", object_name="v")
    header = render_header(meta)
    assert MARKER_OPEN in header
    assert MARKER_CLOSE in header
    assert "object_type: view" in header
    assert "object_name: v" in header


def test_ensure_header_prepends_when_absent() -> None:
    body = "CREATE TABLE s.t (id int);\n"
    result = ensure_header(body, object_catalog="db", object_schema="s", object_type="table", object_name="t")
    assert result.startswith("/*")
    assert MARKER_OPEN in result
    # original body preserved at the end
    assert result.rstrip().endswith("CREATE TABLE s.t (id int);")


def test_ensure_header_idempotent_when_present() -> None:
    body = "CREATE TABLE s.t (id int);\n"
    once = ensure_header(body, object_catalog="db", object_schema="s", object_type="table", object_name="t")
    twice = ensure_header(once, object_catalog="db", object_schema="s", object_type="table", object_name="t")
    # Second call must not add another header.
    assert twice.count(MARKER_OPEN) == 1
    assert twice.count(MARKER_CLOSE) == 1


def test_extract_header_roundtrip() -> None:
    body = "SELECT 1;\n"
    decorated = ensure_header(body, object_catalog="db", object_schema="s", object_type="view", object_name="v")
    parsed = extract_header(decorated)
    assert parsed is not None
    assert parsed["object"]["object_name"] == "v"
    assert parsed["object"]["object_type"] == "view"
    assert parsed["object"]["object_key"] == "pg_database/db/schema/s/type/view/name/v"


def test_extract_header_returns_none_when_absent() -> None:
    assert extract_header("plain SQL without markers") is None


def test_extract_header_returns_none_on_invalid_yaml() -> None:
    """A header with broken YAML should not crash; returns None."""
    broken = f"{MARKER_OPEN}\n  object: {': : :'}\n{MARKER_CLOSE}"
    assert extract_header(broken) is None


# --- object_signature support (Phase 4) ---


def test_build_metadata_with_signature_adds_key_suffix() -> None:
    """A non-empty object_signature appends ``/signature/<hash>`` to object_key."""
    meta = build_metadata(
        object_catalog="db",
        object_schema="app",
        object_type="function",
        object_name="sp_x",
        object_signature="a1b2c3d4",
    )
    assert meta["object"]["object_key"] == (
        "pg_database/db/schema/app/type/function/name/sp_x/signature/a1b2c3d4"
    )
    assert meta["object"]["object_signature"] == "a1b2c3d4"


def test_build_metadata_without_signature_omits_field_and_suffix() -> None:
    """Default (empty) signature: no field, no key suffix — backward compatible."""
    meta = build_metadata(
        object_catalog="db", object_schema="app", object_type="function", object_name="sp_y"
    )
    assert "object_signature" not in meta["object"]
    assert meta["object"]["object_key"] == "pg_database/db/schema/app/type/function/name/sp_y"


def test_build_metadata_table_with_explicit_empty_signature() -> None:
    """Tables never get a signature suffix even if passed explicitly empty."""
    meta = build_metadata(
        object_catalog="db",
        object_schema="bookings",
        object_type="table",
        object_name="aircrafts",
        object_signature="",
    )
    assert meta["object"]["object_key"] == "pg_database/db/schema/bookings/type/table/name/aircrafts"
    assert "object_signature" not in meta["object"]


def test_two_overloads_get_distinct_keys() -> None:
    """Two functions with the same name but different signatures must produce
    different object_keys — the core fix for the silent-overwrite bug."""
    meta_int = build_metadata(
        object_catalog="db", object_schema="app", object_type="function", object_name="sp_x",
        object_signature="a1b2c3d4",
    )
    meta_text = build_metadata(
        object_catalog="db", object_schema="app", object_type="function", object_name="sp_x",
        object_signature="e5f6a7b8",
    )
    assert meta_int["object"]["object_key"] != meta_text["object"]["object_key"]


def test_signature_roundtrip_through_header() -> None:
    """signature written into the header must survive ensure_header -> extract_header."""
    body = "CREATE FUNCTION app.sp_x(int4) RETURNS int4 LANGUAGE sql AS $$ SELECT 1 $$;\n"
    decorated = ensure_header(
        body,
        object_catalog="db",
        object_schema="app",
        object_type="function",
        object_name="sp_x",
        object_signature="a1b2c3d4",
    )
    parsed = extract_header(decorated)
    assert parsed is not None
    assert parsed["object"]["object_signature"] == "a1b2c3d4"
    assert parsed["object"]["object_key"].endswith("/signature/a1b2c3d4")


def test_no_signature_in_header_for_table() -> None:
    """Regression: tables must not carry object_signature in the autodoc."""
    body = "CREATE TABLE bookings.t (id int);\n"
    decorated = ensure_header(
        body, object_catalog="db", object_schema="bookings", object_type="table", object_name="t"
    )
    parsed = extract_header(decorated)
    assert parsed is not None
    assert "object_signature" not in parsed["object"]


# --- extra fields: extension_version / properties (Phase 5) ---


def test_extension_schema_less_key_and_version_roundtrip() -> None:
    """Extensions are schema-less (object_schema=None); the installed version is
    carried informationally (Phase 5 vision Q3 — no VERSION pinning in DDL)."""
    body = 'CREATE EXTENSION IF NOT EXISTS "citext";\n'
    decorated = ensure_header(
        body,
        object_catalog="db",
        object_schema=None,
        object_type="extension",
        object_name="citext",
        extra={"extension_version": "1.6"},
    )
    parsed = extract_header(decorated)
    assert parsed is not None
    assert parsed["object"]["object_key"] == "pg_database/db/type/extension/name/citext"
    assert parsed["object"]["extension_version"] == "1.6"


def test_database_setting_properties_roundtrip() -> None:
    """db-level CREATE DATABASE properties survive ensure_header -> extract_header.
    Roundtrip (not substring) — YAML quoting is an implementation detail
    (LESSONS_LEARNED §28)."""
    body = "ALTER DATABASE mydb SET work_mem = '64MB';\n"
    properties = {"encoding": "UTF8", "lc_collate": "C", "lc_ctype": "C", "template": "template0"}
    decorated = ensure_header(
        body,
        object_catalog="db",
        object_schema=None,
        object_type="database_setting",
        object_name="database settings",
        extra={"properties": properties},
    )
    parsed = extract_header(decorated)
    assert parsed is not None
    assert parsed["object"]["object_key"] == (
        "pg_database/db/type/database_setting/name/database settings"
    )
    assert parsed["object"]["properties"] == properties


def test_extra_collision_with_standard_fields_rejected() -> None:
    """extra must not silently overwrite object identity fields."""
    import pytest

    with pytest.raises(ValueError, match="collide"):
        build_metadata(
            object_catalog="db",
            object_schema=None,
            object_type="extension",
            object_name="citext",
            extra={"object_name": "hacked"},
        )


def test_no_extra_keeps_header_unchanged() -> None:
    """Backward compatibility: without extra the metadata has exactly the
    standard fields (plus optional signature)."""
    meta = build_metadata(
        object_catalog="db", object_schema="s", object_type="table", object_name="t"
    )
    assert set(meta["object"]) == {
        "object_catalog", "object_schema", "object_type", "object_name", "object_key",
    }


# --- update_header: mutate existing header in place (Phase 6) ---


def test_update_header_adds_qualify_report() -> None:
    """update_header mutates the parsed metadata and re-renders the YAML block,
    preserving the SQL body and the comment wrapper."""
    body = "SELECT 1;\n"
    decorated = ensure_header(
        body, object_catalog="db", object_schema="s", object_type="view", object_name="v"
    )

    def _add_qualify(metadata: dict) -> None:
        metadata["qualify_report"] = ["sp_x", "users"]

    updated = update_header(decorated, _add_qualify)
    parsed = extract_header(updated)
    assert parsed is not None
    assert parsed["qualify_report"] == ["sp_x", "users"]
    # Body preserved verbatim.
    assert updated.rstrip().endswith("SELECT 1;")
    # No duplicated markers.
    assert updated.count(MARKER_OPEN) == 1
    assert updated.count(MARKER_CLOSE) == 1


def test_update_header_preserves_existing_fields() -> None:
    """Existing object fields (object_type, object_name, ...) survive mutation."""
    body = "CREATE TABLE s.t (id int);\n"
    decorated = ensure_header(
        body, object_catalog="db", object_schema="s", object_type="table", object_name="t"
    )

    def _noop(metadata: dict) -> None:
        metadata["new_field"] = "value"

    updated = update_header(decorated, _noop)
    parsed = extract_header(updated)
    assert parsed is not None
    assert parsed["object"]["object_type"] == "table"
    assert parsed["object"]["object_name"] == "t"
    assert parsed["new_field"] == "value"


def test_update_header_noop_without_header() -> None:
    """No header → script returned unchanged."""
    plain = "SELECT 1;\n"
    assert update_header(plain, lambda m: m.update({"x": 1})) == plain


def test_update_header_extends_existing_qualify_report() -> None:
    """Running update_header twice appends rather than overwrites."""
    body = "SELECT 1;\n"
    decorated = ensure_header(
        body, object_catalog="db", object_schema="s", object_type="view", object_name="v"
    )
    # First pass: add one ref.
    decorated = update_header(decorated, lambda m: m.update({"qualify_report": ["a"]}))
    # Second pass: append another.
    decorated = update_header(
        decorated,
        lambda m: m.setdefault("qualify_report", []).append("b"),
    )
    parsed = extract_header(decorated)
    assert parsed is not None
    assert parsed["qualify_report"] == ["a", "b"]


# --- immutable marker (Phase 10 / CDF-10) ---


def test_build_metadata_omits_immutable_when_false() -> None:
    """Default immutable=False: 'immutable' must NOT appear in project section.

    Keeps ordinary objects' headers clean (CDF-10: omit-when-false).
    """
    meta = build_metadata(
        object_catalog="db", object_schema="s", object_type="table", object_name="t"
    )
    assert meta["project"] == {"build": True}
    assert "immutable" not in meta["project"]


def test_build_metadata_includes_immutable_when_true() -> None:
    """immutable=True: 'immutable: true' added to project section."""
    meta = build_metadata(
        object_catalog="db",
        object_schema="__deploy",
        object_type="table",
        object_name="schema_version",
        immutable=True,
    )
    assert meta["project"] == {"build": True, "immutable": True}


def test_ensure_header_propagates_immutable_through_roundtrip() -> None:
    """immutable=True written via ensure_header survives extract_header."""
    body = "CREATE TABLE __deploy.schema_version (id int);\n"
    decorated = ensure_header(
        body,
        object_catalog="db",
        object_schema="__deploy",
        object_type="table",
        object_name="schema_version",
        immutable=True,
    )
    parsed = extract_header(decorated)
    assert parsed is not None
    assert parsed["project"]["immutable"] is True


def test_ensure_header_default_immutable_absent_in_roundtrip() -> None:
    """Default immutable=False: parsed header's project has no 'immutable' key."""
    body = "CREATE TABLE s.t (id int);\n"
    decorated = ensure_header(
        body, object_catalog="db", object_schema="s", object_type="table", object_name="t"
    )
    parsed = extract_header(decorated)
    assert parsed is not None
    assert "immutable" not in parsed["project"]
    assert parsed["project"] == {"build": True}


def test_vertex_defaults_immutable_false() -> None:
    """Vertex model defaults immutable to False when not passed (parser fallback path)."""
    from db_project_manager.domain.graph import Vertex

    v = Vertex(object_key="pg_database/db/schema/s/type/table/name/t", object_type="table")
    assert v.immutable is False


def test_vertex_accepts_immutable_true() -> None:
    """Vertex carries the immutable flag for __deploy objects."""
    from db_project_manager.domain.graph import Vertex

    v = Vertex(
        object_key="pg_database/db/schema/__deploy/type/table/name/schema_version",
        object_type="table",
        immutable=True,
    )
    assert v.immutable is True


def test_parser_reads_immutable_from_autodoc(tmp_path) -> None:
    """pg_sql_parser extracts project.immutable into Vertex (CDF-10 wiring)."""
    from db_project_manager.infrastructure.parsing.pg_sql_parser import PgSqlParser

    body = (
        f"{MARKER_OPEN}\n"
        "object:\n"
        "  object_catalog: db\n"
        "  object_schema: __deploy\n"
        "  object_type: table\n"
        "  object_name: schema_version\n"
        "  object_key: pg_database/db/schema/__deploy/type/table/name/schema_version\n"
        "project:\n"
        "  build: true\n"
        "  immutable: true\n"
        f"{MARKER_CLOSE}\n"
        "*/\n"
        "CREATE TABLE __deploy.schema_version (id int);"
    )
    f = tmp_path / "schema_version.sql"
    f.write_text(body, encoding="utf-8")
    parser = PgSqlParser()
    vertex, _, _ = parser._parse_file(f, tmp_path)
    assert vertex is not None
    assert vertex.immutable is True
    assert vertex.object_schema == "__deploy"


def test_parser_defaults_immutable_false_when_absent(tmp_path) -> None:
    """Ordinary autodoc (no immutable field) → Vertex.immutable=False."""
    from db_project_manager.infrastructure.parsing.pg_sql_parser import PgSqlParser

    body = (
        f"{MARKER_OPEN}\n"
        "object:\n"
        "  object_catalog: db\n"
        "  object_schema: bookings\n"
        "  object_type: table\n"
        "  object_name: aircrafts\n"
        "  object_key: pg_database/db/schema/bookings/type/table/name/aircrafts\n"
        "project:\n"
        "  build: true\n"
        f"{MARKER_CLOSE}\n"
        "*/\n"
        "CREATE TABLE bookings.aircrafts (id int);"
    )
    f = tmp_path / "aircrafts.sql"
    f.write_text(body, encoding="utf-8")
    parser = PgSqlParser()
    vertex, _, _ = parser._parse_file(f, tmp_path)
    assert vertex is not None
    assert vertex.immutable is False

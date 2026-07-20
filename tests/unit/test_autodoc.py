"""Tests for db_project_manager.infrastructure.sql.autodoc."""

from __future__ import annotations

from db_project_manager.infrastructure.sql.autodoc import (
    MARKER_CLOSE,
    MARKER_OPEN,
    build_metadata,
    ensure_header,
    extract_header,
    render_header,
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

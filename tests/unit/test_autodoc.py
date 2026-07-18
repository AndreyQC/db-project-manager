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

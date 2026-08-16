"""Tests for db_project_manager.infrastructure.diff.columns (Phase 12, step S2).

Covers: extraction from canonical RE-DDL and hand-written DDL, type-synonym
canonicalization (the ALT-1b price), fail-safe `None` cases (PARTITION OF, broken SQL),
inline table constraints not leaking into columns, snapshot-builder integration, and
the hash-stability invariant (normalize_sql/sql_hash untouched — LESSONS §44 spirit).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from db_project_manager.domain.diff import SnapshotSourceKind
from db_project_manager.infrastructure.diff.columns import (
    canonical_type,
    extract_columns,
)
from db_project_manager.infrastructure.diff.normalize_sql import normalize_sql, sql_hash
from db_project_manager.infrastructure.diff.snapshot import (
    _read_sql_body,
    build_snapshot_from_dir,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
CODEBASE_SAMPLE = FIXTURES / "codebase_sample"


def _extract(ddl: str):
    return extract_columns(ddl)


# ------------------------------------------------- canonical RE-generated DDL


def test_extract_from_canonical_re_ddl() -> None:
    """Fixture file: bpchar(3)/text/int4 + NOT NULL; COMMENT ON lines after CREATE."""
    body = _read_sql_body(CODEBASE_SAMPLE, "bookings/tables/table aircrafts.sql")
    cols = _extract(body)
    assert cols is not None
    by_name = {c.name: c for c in cols}
    assert by_name["aircraft_code"].type == "char(3)"       # bpchar → char alias
    assert by_name["aircraft_code"].nullable is False
    assert by_name["model"].type == "text"
    assert by_name["range"].type == "int"
    assert all(c.default is None for c in cols)


def test_extract_default_expression() -> None:
    """Sequence default: rendered expression text (sqlglot-normalized, symmetric)."""
    body = _read_sql_body(CODEBASE_SAMPLE, "bookings/tables/table tickets.sql")
    cols = _extract(body)
    assert cols is not None
    ticket_id = next(c for c in cols if c.name == "ticket_id")
    assert ticket_id.default is not None
    assert "nextval" in ticket_id.default.lower()
    assert "tickets_id_seq" in ticket_id.default


# ------------------------------------------------------------- hand-written DDL


def test_extract_hand_written_aliases_and_defaults() -> None:
    ddl = """
    CREATE TABLE app.orders (
        id integer NOT NULL DEFAULT 42,
        note character varying(40) DEFAULT 'n/a',
        created timestamptz DEFAULT now(),
        amount numeric(10,2),
        flag bool
    );
    """
    cols = _extract(ddl)
    assert cols is not None
    by_name = {c.name: c for c in cols}
    assert by_name["id"].type == "int"
    assert by_name["id"].nullable is False
    assert by_name["id"].default == "42"
    assert by_name["note"].type == "varchar(40)"
    assert by_name["note"].nullable is True
    assert by_name["note"].default == "'n/a'"
    assert by_name["created"].type == "timestamptz"
    # sqlglot canonicalizes now() to CURRENT_TIMESTAMP — fine, both sides render the same
    assert by_name["created"].default is not None
    assert "current_timestamp" in by_name["created"].default.lower()
    assert by_name["amount"].type == "decimal(10,2)"
    assert by_name["flag"].type == "boolean"


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("integer", "int4"),
        ("int", "int4"),
        ("bigint", "int8"),
        ("smallint", "int2"),
        ("char(3)", "bpchar(3)"),
        ("character varying(10)", "varchar(10)"),
        ("timestamp with time zone", "timestamptz"),
        ("numeric(10,2)", "decimal(10,2)"),
        ("boolean", "bool"),
    ],
)
def test_type_synonyms_collapse(left: str, right: str) -> None:
    """integer-in-code vs int4-in-RE must NOT produce a TYPE_CHANGED downstream."""
    lcols = _extract(f"CREATE TABLE t (c {left});")
    rcols = _extract(f"CREATE TABLE t (c {right});")
    assert lcols is not None and rcols is not None
    assert lcols[0].type == rcols[0].type


def test_canonical_type_collapses_whitespace() -> None:
    assert canonical_type(_kind_of("numeric(10,2)")) == "decimal(10,2)"


def _kind_of(type_text: str):
    from sqlglot import exp, parse_one

    tree = parse_one(f"CREATE TABLE t (c {type_text});", read="postgres")
    col = tree.find(exp.ColumnDef)
    assert col is not None and col.kind is not None
    return col.kind


# ---------------------------------------------------------------- fail-safe None


@pytest.mark.parametrize(
    "body",
    [
        "",
        "   ",
        "CREATE VIEW app.v AS SELECT 1;",                 # not a table Create
        "CREATE TABLE app.child PARTITION OF app.parent;",  # no column list
        "THIS IS NOT SQL AT ALL ((( ;",
        "CREATE TABLE t ();",                              # empty column list
    ],
)
def test_unextractable_bodies_return_none(body: str) -> None:
    assert _extract(body) is None


def test_column_without_type_returns_none() -> None:
    # A ColumnDef without a kind means we can't trust the extraction at all.
    assert _extract("CREATE TABLE t (c NOT NULL);") is None


# ---------------------------------------------------- inline table constraints


def test_inline_constraints_do_not_become_columns() -> None:
    ddl = """
    CREATE TABLE app.items (
        id integer NOT NULL,
        order_id integer NOT NULL REFERENCES app.orders (id),
        n numeric(10,2) CHECK (n > 0),
        CONSTRAINT items_pkey PRIMARY KEY (id)
    );
    """
    cols = _extract(ddl)
    assert cols is not None
    assert [c.name for c in cols] == ["id", "order_id", "n"]
    nullable_by_name = {c.name: c.nullable for c in cols}
    assert nullable_by_name == {"id": False, "order_id": False, "n": True}


# -------------------------------------------------------- snapshot integration


def _build_sample_snapshot():
    return build_snapshot_from_dir(
        CODEBASE_SAMPLE,
        source_kind=SnapshotSourceKind.DIR,
        source_ref=str(CODEBASE_SAMPLE),
        db_type="postgres",
    )


def test_snapshot_tables_have_columns_others_none() -> None:
    snap = _build_sample_snapshot()
    tables = [o for o in snap.objects.values() if o.object_type == "table"]
    others = [o for o in snap.objects.values() if o.object_type != "table"]
    assert tables, "fixture must contain tables"
    for t in tables:
        assert t.columns is not None, f"table {t.object_name} expected to have columns"
    for o in others:
        assert o.columns is None, f"{o.object_type} {o.object_name} must have columns=None"


def test_snapshot_columns_parse_back() -> None:
    """Snapshot with columns survives a pydantic round-trip (LESSONS §28)."""
    snap = _build_sample_snapshot()
    restored = type(snap).model_validate_json(snap.model_dump_json())
    table = next(o for o in restored.objects.values() if o.object_type == "table")
    assert table.columns is not None
    assert all(c.name for c in table.columns)


# ------------------------------------------------------- hash-stability invariant

#: Reference hashes of the fixture bodies — captured BEFORE the Phase 12 snapshot
#: integration. If one of these changes, normalize_sql/sql_hash was accidentally
#: modified and every existing object would show up as CHANGED (plan S2 invariant).
_REFERENCE_HASHES = {
    "bookings/tables/table aircrafts.sql": "dc5c19a1",
    "bookings/tables/table flights.sql": "382eb2de",
    "bookings/tables/table tickets.sql": "5145fac0",
}


@pytest.mark.parametrize(("rel_path", "expected_hash"), sorted(_REFERENCE_HASHES.items()))
def test_sql_hash_unchanged_by_column_extraction(rel_path: str, expected_hash: str) -> None:
    body = _read_sql_body(CODEBASE_SAMPLE, rel_path)
    assert sql_hash(normalize_sql(body)) == expected_hash
    # extraction itself must not influence the hashing input
    _extract(body)
    assert sql_hash(normalize_sql(body)) == expected_hash

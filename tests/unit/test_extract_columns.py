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
    ColumnChangeKind,
    ColumnSnapshot,
    canonical_type,
    diff_columns,
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


def test_extract_from_drop_plus_create_body() -> None:
    """Regression (feedback 01.09): RE-style files start with
    ``DROP TABLE IF EXISTS ... CASCADE;`` — parse_one returned the Drop and the
    whole file degraded to "columns unavailable" (0 columns in yaml generate).
    The CREATE must be found among ALL statements."""
    body = """DROP TABLE IF EXISTS s.t CASCADE;

CREATE TABLE s.t (
    id INT NOT NULL,
    name TEXT,
    amount NUMERIC (38, 0) DEFAULT 0
);

COMMENT ON TABLE s.t IS 'x';"""
    cols = _extract(body)
    assert cols is not None
    by_name = {c.name: c for c in cols}
    assert by_name["id"].nullable is False
    assert by_name["amount"].type == "decimal(38,0)"
    assert by_name["amount"].default == "0"


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


def test_explicit_null_marker_means_nullable() -> None:
    """RE renders `"col" text NULL` for nullable columns; sqlglot models the
    explicit NULL as NotNullColumnConstraint(allow_null=True) — must NOT be
    treated as NOT NULL (found in S8 integration, LESSONS §44)."""
    ddl = 'CREATE TABLE "app"."orders" (\n    "id" int4 NOT NULL,\n    "note" text NULL\n);'
    cols = _extract(ddl)
    assert cols is not None
    by_name = {c.name: c for c in cols}
    assert by_name["id"].nullable is False
    assert by_name["note"].nullable is True


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


# --- Phase 15.5.4: serial/int family aliases (cis_zup feedback 2026-09-04) ---


@pytest.mark.parametrize(
    ("alias_a", "alias_b"),
    [
        ("serial4", "int4"),
        ("serial4", "int"),
        ("serial8", "int8"),
        ("serial8", "bigint"),
        ("serial2", "int2"),
        ("serial2", "smallint"),
        ("float4", "real"),
        ("float8", "double precision"),
    ],
)
def test_pg_type_aliases_collapse_in_column_extraction(alias_a: str, alias_b: str) -> None:
    """PG synonym tables that sqlglot does NOT collapse must be unified
    by ``_TYPE_ALIASES`` (Phase 15.5.4).
    """
    a_cols = _extract(f"CREATE TABLE t (c {alias_a} NOT NULL)")
    b_cols = _extract(f"CREATE TABLE t (c {alias_b} NOT NULL)")
    assert a_cols is not None and b_cols is not None
    assert a_cols[0].type == b_cols[0].type, (
        f"PG synonyms {alias_a!r} vs {alias_b!r} should compare equal but got "
        f"{a_cols[0].type!r} vs {b_cols[0].type!r}"
    )


# --- Phase 15.5.4: serial vs int+nextval default equivalence ---


def _snap(ddl: str) -> list[ColumnSnapshot]:
    cols = _extract(ddl)
    assert cols is not None
    return cols


def test_serial_vs_int_plus_nextval_yields_no_column_diff():
    """cis_zup regression: ``process_log_id serial4`` (codebase) vs
    ``int + DEFAULT NEXTVAL(...)`` (DB round-trip) must NOT produce
    TYPE_CHANGED + DEFAULT_CHANGED — they're the same serial column.
    """
    src = _snap("CREATE TABLE t (id serial4 NOT NULL)")
    tgt = _snap(
        "CREATE TABLE t (id int NOT NULL DEFAULT nextval('public.t_id_seq'::REGCLASS))"
    )
    diffs = diff_columns(src, tgt)
    assert diffs == [], f"expected no diffs for serial vs int+nextval, got {diffs!r}"


def test_serial_vs_int_without_nextval_produces_no_diff():
    """serial4 NOT NULL vs int NOT NULL — both default-less. The serial
    implicit nextval vs int empty default is indistinguishable from our
    extract_columns output (both yield ``default=None`` after sqlglot). The
    Phase 15.5.4 compensation only fires when one side HAS an explicit
    nextval and the other doesn't — here BOTH lack explicit default, so no
    diff is emitted. Documented limitation in LESSONS §64.
    """
    src = _snap("CREATE TABLE t (id serial4 NOT NULL)")
    tgt = _snap("CREATE TABLE t (id int NOT NULL)")  # no DEFAULT nextval
    diffs = diff_columns(src, tgt)
    assert diffs == [], (
        "both default-less columns canonicalise equal; "
        f"expected no diffs, got {diffs!r}"
    )


def test_real_serial_hand_written_differs_from_int_with_unrelated_default():
    """Negative control: int column with a non-nextval default truly differs
    from serial (no diagnostic false-negative).
    """
    src = _snap("CREATE TABLE t (id serial4 NOT NULL)")
    tgt = _snap("CREATE TABLE t (id int NOT NULL DEFAULT 42)")
    diffs = diff_columns(src, tgt)
    # 42 is not a NEXTVAL — diff must NOT be hidden.
    assert any(d.kind in (ColumnChangeKind.TYPE_CHANGED, ColumnChangeKind.DEFAULT_CHANGED) for d in diffs)


def test_default_compensation_only_for_serial_type_match():
    """Default-compensation should not hide real differences when types are
    genuinely different (e.g. serial4 vs text).
    """
    src = _snap("CREATE TABLE t (id serial4 NOT NULL)")
    tgt = _snap("CREATE TABLE t (id text NOT NULL DEFAULT nextval('public.t_id_seq'::REGCLASS))")
    diffs = diff_columns(src, tgt)
    # type_changed + nullability stays as-is. The nextval-compensation is
    # gated on type being a serial/int pair, not a generic nextval.
    types_differ = any(d.kind is ColumnChangeKind.TYPE_CHANGED for d in diffs)
    assert types_differ, "serial4 vs text must remain TYPE_CHANGED"


def test_text_cast_default_canonicalized_in_extraction():
    """Phase 15.7 (cis_zup 2026-09-04): ``'x'::text`` / ``CAST('x' AS TEXT)`` in a
    DEFAULT must extract to the same canonical ``'x'`` as a plain literal — the
    column path was missing the §63 text-cast canonicalisation that normalize_sql
    applies to the body hash, producing a false DEFAULT_CHANGED on event_datetime.
    """
    a = _extract("CREATE TABLE t (c TIMESTAMP NOT NULL DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'UTC'))")
    b = _extract("CREATE TABLE t (c TIMESTAMP NOT NULL DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'UTC'::text))")
    c = _extract("CREATE TABLE t (c TIMESTAMP NOT NULL DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE CAST('UTC' AS TEXT)))")
    assert a and b and c
    assert a[0].default == b[0].default == c[0].default
    assert "CAST" not in (c[0].default or "")
    assert diff_columns(a, b) == []
    assert diff_columns(a, c) == []


def test_cis_zup_serial_and_text_cast_combined_no_diff():
    """The full cis_zup table shape: serial4 + 'UTC' (source) vs
    int + NEXTVAL(...) + 'UTC'::text (target) must yield an EMPTY column diff.
    """
    src = _snap(
        'CREATE TABLE t (id serial4 NOT NULL, '
        "ev TIMESTAMP NOT NULL DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'UTC'))"
    )
    tgt = _snap(
        "CREATE TABLE t (id INT NOT NULL DEFAULT nextval('public.t_id_seq'::REGCLASS), "
        "ev TIMESTAMP NOT NULL DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'UTC'::text))"
    )
    assert diff_columns(src, tgt) == []


def test_bare_serial_is_int_and_not_null():
    """Phase 15.7 (__deploy schema): ``id SERIAL PRIMARY KEY`` (canonical DDL) must
    canonicalise to ``int NOT NULL`` — SERIAL is ``int NOT NULL DEFAULT nextval``.
    """
    cols = _snap('CREATE TABLE t (id SERIAL PRIMARY KEY)')
    assert cols[0].type == "int"
    assert cols[0].nullable is False


def test_bare_serial_vs_re_roundtrip_no_diff():
    """The __deploy table false-positive: ``id SERIAL PRIMARY KEY`` vs the RE form
    ``id int4 NOT NULL DEFAULT nextval(...)`` must produce no column diffs.
    """
    src = _snap('CREATE TABLE "__deploy"."schema_version" (id SERIAL PRIMARY KEY, version TEXT NOT NULL)')
    tgt = _snap(
        "CREATE TABLE \"__deploy\".\"schema_version\" "
        "(id int4 NOT NULL DEFAULT nextval('__deploy.schema_version_id_seq'::REGCLASS), version TEXT NOT NULL)"
    )
    assert diff_columns(src, tgt) == []

"""Tests for db_project_manager.infrastructure.sql.sql_generator."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from db_project_manager.infrastructure.sql.sql_generator import SQLGenerator

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


@pytest.fixture
def structure() -> dict:
    with (FIXTURES / "sample_structure.json").open("r", encoding="utf-8") as f:
        return json.load(f)


def test_generate_creates_expected_tree(structure, tmp_path) -> None:
    gen = SQLGenerator()
    out = gen.generate_scripts(structure, tmp_path / "out")

    # schema dir
    assert (out / "bookings").is_dir()
    # schema script (non-public schema gets one)
    assert (out / "bookings" / "schema bookings.sql").is_file()
    # tables
    tables_dir = out / "bookings" / "tables"
    assert (tables_dir / "table aircrafts.sql").is_file()
    assert (tables_dir / "table flights.sql").is_file()
    # views
    assert (out / "bookings" / "views" / "view flights_v.sql").is_file()


def test_table_script_contains_columns_and_pk(structure, tmp_path) -> None:
    gen = SQLGenerator()
    out = gen.generate_scripts(structure, tmp_path / "out")
    text = (out / "bookings" / "tables" / "table aircrafts.sql").read_text(encoding="utf-8")

    assert "CREATE TABLE bookings.aircrafts" in text
    assert "aircraft_code bpchar(3) NOT NULL" in text
    assert "range int4 NOT NULL" in text
    assert "CONSTRAINT aircrafts_pkey PRIMARY KEY (aircraft_code)" in text
    assert "CHECK ((range > 0))" in text
    # table comment + column comments
    assert "COMMENT ON TABLE bookings.aircrafts IS 'Самолеты'" in text
    assert "COMMENT ON COLUMN bookings.aircrafts.aircraft_code IS 'Код самолета, IATA'" in text


def test_table_fk_uses_f_comment_not_c(structure, tmp_path) -> None:
    """Regression: the FK section referenced {{ c.comment }} while iterating f.

    With the fix it must render the FK's own comment, not error out.
    """
    gen = SQLGenerator()
    out = gen.generate_scripts(structure, tmp_path / "out")
    text = (out / "bookings" / "tables" / "table flights.sql").read_text(encoding="utf-8")

    assert "ALTER TABLE bookings.flights ADD CONSTRAINT flights_aircraft_code_fkey" in text
    assert "REFERENCES bookings.aircrafts(aircraft_code)" in text
    # FK comment is rendered
    assert "FK to aircrafts" in text


def test_default_not_rendered_when_none(structure, tmp_path) -> None:
    """Regression: 'col.default != None' string comparison produced 'DEFAULT None'.

    A column with default=null must not emit a DEFAULT clause.
    """
    gen = SQLGenerator()
    out = gen.generate_scripts(structure, tmp_path / "out")
    text = (out / "bookings" / "tables" / "table aircrafts.sql").read_text(encoding="utf-8")

    assert "DEFAULT None" not in text
    # aircraft_code has default null -> no DEFAULT keyword for it
    assert "aircraft_code bpchar(3) NOT NULL\n" in text or "aircraft_code bpchar(3) NOT NULL," in text


def test_default_rendered_when_present(structure, tmp_path) -> None:
    gen = SQLGenerator()
    out = gen.generate_scripts(structure, tmp_path / "out")
    text = (out / "bookings" / "tables" / "table flights.sql").read_text(encoding="utf-8")
    assert "DEFAULT nextval('flights_flight_id_seq'::regclass)" in text


def test_view_script(structure, tmp_path) -> None:
    gen = SQLGenerator()
    out = gen.generate_scripts(structure, tmp_path / "out")
    text = (out / "bookings" / "views" / "view flights_v.sql").read_text(encoding="utf-8")
    assert "CREATE OR REPLACE VIEW bookings.flights_v" in text
    assert "flight_id, aircraft_code" in text
    assert "COMMENT ON VIEW bookings.flights_v IS 'Представление рейсов'" in text


def test_empty_structure_produces_no_files(tmp_path) -> None:
    gen = SQLGenerator()
    out = gen.generate_scripts({"schemas": []}, tmp_path / "out")
    assert out.exists()
    assert list(out.iterdir()) == []


def test_autodoc_header_prepended_by_default(structure, tmp_path) -> None:
    gen = SQLGenerator()
    out = gen.generate_scripts(structure, tmp_path / "out", object_catalog="mydb")
    text = (out / "bookings" / "tables" / "table aircrafts.sql").read_text(encoding="utf-8")
    assert "[<[autodoc-yaml]]" in text
    assert "[[autodoc-yaml]>]" in text
    assert "object_catalog: mydb" in text
    assert "object_type: table" in text
    assert "object_name: aircrafts" in text
    assert "object_key: pg_database/mydb/schema/bookings/type/table/name/aircrafts" in text


def test_autodoc_disabled(structure, tmp_path) -> None:
    gen = SQLGenerator(autodoc=False)
    out = gen.generate_scripts(structure, tmp_path / "out", object_catalog="mydb")
    text = (out / "bookings" / "tables" / "table aircrafts.sql").read_text(encoding="utf-8")
    assert "[<[autodoc-yaml]]" not in text
    # Body still intact.
    assert "CREATE TABLE bookings.aircrafts" in text


# -------------------------------------------------------------------------- #
# type_mod helpers (tested in isolation via a minimal generator instance)
# -------------------------------------------------------------------------- #

import pytest
from db_project_manager.infrastructure.sql.sql_generator import SQLGenerator, _NO_NUMERIC_MOD


def _type_mod_for(type_: str, np=None, ns=None, cml=None) -> str:
    """Render type_mod for a column dict with the given attributes."""
    gen = SQLGenerator()
    return gen.env.globals["_type_mod"]({
        "type": type_,
        "numeric_precision": np,
        "numeric_scale": ns,
        "character_maximum_length": cml,
    })


class TestTypeModIntegerTypes:
    """Bug A: int8/int4 with numeric_precision should NOT emit (64, 0)."""

    @pytest.mark.parametrize("udt", ["int8", "int4", "int2", "bigint", "integer", "smallint"])
    def test_integer_type_no_modifier(self, udt) -> None:
        """Even when PostgreSQL reports precision/scale, they must not appear in DDL."""
        result = _type_mod_for(udt, np=64, ns=0)
        assert result == "", f"{udt} must not get (precision, scale) suffix"

    def test_bigserial_no_modifier(self) -> None:
        result = _type_mod_for("bigserial", np=64, ns=0)
        assert result == ""

    def test_smallserial_no_modifier(self) -> None:
        result = _type_mod_for("smallserial", np=16, ns=0)
        assert result == ""


class TestTypeModFloatTypes:
    """float4/float8 do not accept (precision, scale) in PostgreSQL DDL."""

    def test_float4_no_modifier(self) -> None:
        result = _type_mod_for("float4", np=24, ns=0)
        assert result == ""

    def test_float8_no_modifier(self) -> None:
        result = _type_mod_for("float8", np=53, ns=0)
        assert result == ""

    def test_real_no_modifier(self) -> None:
        result = _type_mod_for("real", np=24, ns=0)
        assert result == ""

    def test_double_precision_no_modifier(self) -> None:
        result = _type_mod_for("double precision", np=53, ns=0)
        assert result == ""


class TestTypeModDateTimeTypes:
    """timestamp/date/time types do not accept (precision, scale) modifiers."""

    def test_timestamp_no_modifier(self) -> None:
        result = _type_mod_for("timestamp", np=6, ns=6)
        assert result == ""

    def test_timestamptz_no_modifier(self) -> None:
        result = _type_mod_for("timestamptz", np=6, ns=6)
        assert result == ""

    def test_date_no_modifier(self) -> None:
        result = _type_mod_for("date", np=4, ns=0)
        assert result == ""


class TestTypeModNumeric:
    """numeric/decimal ARE valid with (precision, scale)."""

    def test_numeric_emits_modifier(self) -> None:
        result = _type_mod_for("numeric", np=10, ns=2)
        assert result == "(10, 2)"

    def test_decimal_emits_modifier(self) -> None:
        result = _type_mod_for("decimal", np=15, ns=3)
        assert result == "(15, 3)"


class TestTypeModCharacter:
    """Character types emit character_maximum_length, not numeric_precision/scale."""

    def test_varchar_emits_length(self) -> None:
        result = _type_mod_for("varchar", np=None, ns=None, cml=255)
        assert result == "(255)"

    def test_bpchar_emits_length(self) -> None:
        result = _type_mod_for("bpchar", np=None, ns=None, cml=3)
        assert result == "(3)"

    def test_text_no_modifier(self) -> None:
        result = _type_mod_for("text", np=100, ns=0)
        assert result == ""

    def test_bpchar_no_cml_no_modifier(self) -> None:
        result = _type_mod_for("bpchar", np=None, ns=None, cml=None)
        assert result == ""


class TestNoNumericModSet:
    """Sanity-check the frozenset covers the expected PostgreSQL types."""

    def test_no_numeric_mod_set_contains_int8(self) -> None:
        assert "int8" in _NO_NUMERIC_MOD

    def test_no_numeric_mod_set_contains_numeric(self) -> None:
        # numeric IS allowed to have (precision, scale)
        assert "numeric" not in _NO_NUMERIC_MOD

    def test_no_numeric_mod_set_contains_bool(self) -> None:
        assert "bool" in _NO_NUMERIC_MOD

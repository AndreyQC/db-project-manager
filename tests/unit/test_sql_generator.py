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
    assert "range int4(32, 0) NOT NULL" in text
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

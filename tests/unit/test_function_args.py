"""Argument-type extraction from routine DDL (Phase 16.9).

The extractor feeds signature reconstruction for codebases whose autodoc
predates ``object_signature`` (identity-split into added+removed pairs —
67 functions on cis_zup, 2026-09-14). Its output must be byte-compatible
with the adapter's catalog form (``pg_type.typname``), which is what
``signature_hash`` hashes on the fresh-RE side.
"""

from __future__ import annotations

from pathlib import Path

from db_project_manager.domain.signature import signature_hash
from db_project_manager.infrastructure.parsing.function_args import extract_argument_types


def test_single_json_arg() -> None:
    """The dominant cis_zup ETL-function shape (matches catalog 'json')."""
    body = """CREATE OR REPLACE FUNCTION cis_dmt_zup.fun_lu_zup_accountgroups_etl(
    p_parameters_json json)
RETURNS json LANGUAGE plpgsql VOLATILE SECURITY DEFINER AS $$
BEGIN
    RETURN NULL;
END
$$
EXECUTE ON ANY;"""
    assert extract_argument_types(body) == "json"


def test_ddl_spellings_fold_to_typnames() -> None:
    body = (
        "CREATE FUNCTION s.f(p_a character varying, p_b integer, "
        "p_c timestamp without time zone, p_d boolean, p_e numeric(10,2)) "
        "RETURNS void AS $$ ... $$;"
    )
    assert extract_argument_types(body) == "varchar,int4,timestamp,bool,numeric"


def test_default_expressions_dropped() -> None:
    body = (
        "CREATE FUNCTION s.f(p_limit integer DEFAULT 100, p_name text DEFAULT 'x,y') "
        "RETURNS void AS $$ ... $$;"
    )
    assert extract_argument_types(body) == "int4,text"


def test_out_params_excluded_inout_kept() -> None:
    """PG proargtypes keeps IN/INOUT/VARIADIC and drops OUT — mirror that."""
    body = (
        "CREATE FUNCTION s.f(IN p_a int, OUT p_b text, INOUT p_c date) "
        "RETURNS void AS $$ ... $$;"
    )
    assert extract_argument_types(body) == "int4,date"


def test_variadic_array_type() -> None:
    """Array args: pg_type typnames are _text/_int4 (legacy underscore form)."""
    body = "CREATE FUNCTION s.f(VARIADIC p_items text[]) RETURNS void AS $$ ... $$;"
    assert extract_argument_types(body) == "_text"


def test_schema_qualified_rowtype() -> None:
    """Table rowtype arg: typname is the bare relation name."""
    body = "CREATE FUNCTION s.f(p_row cis_dmt_zup.lu_zup_employee) RETURNS void AS $$ ... $$;"
    assert extract_argument_types(body) == "lu_zup_employee"


def test_no_args_and_no_header() -> None:
    assert extract_argument_types("CREATE FUNCTION s.f() RETURNS void AS $$ ... $$;") == ""
    assert extract_argument_types("SELECT 1") == ""


def test_signature_matches_fresh_re_side() -> None:
    """End-to-end contract: body-extracted signature == catalog-side hash."""
    body = "CREATE OR REPLACE FUNCTION s.f(p_parameters_json json) RETURNS json AS $$ ... $$;"
    assert signature_hash(extract_argument_types(body)) == signature_hash("json")


# --- parser-level reconstruction (Phase 16.9) ---


def _old_style_function_file(tmp_path, root: Path) -> Path:
    """Autodoc WITHOUT object_signature/argument_types (old-generation)."""
    content = """/*====================================================================================
[<[autodoc-yaml]]
object:
  object_catalog: cis_zup
  object_schema: cis_dmt_zup
  object_type: function
  object_name: fun_etl
  object_key: pg_database/cis_zup/schema/cis_dmt_zup/type/function/name/fun_etl
project:
  build: true
[[autodoc-yaml]>]
=====================================================================================*/

CREATE OR REPLACE FUNCTION cis_dmt_zup.fun_etl(p_parameters_json json)
RETURNS json LANGUAGE plpgsql AS $$
BEGIN
    RETURN NULL;
END
$$;
"""
    d = root / "cis_dmt_zup" / "functions"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "function fun_etl.sql"
    f.write_text(content, encoding="utf-8")
    return f


def test_parser_reconstructs_signature_for_old_autodoc(tmp_path):
    from db_project_manager.infrastructure.parsing.pg_sql_parser import PgSqlParser

    _old_style_function_file(tmp_path, tmp_path)
    graph = PgSqlParser().parse_directory(tmp_path)

    assert len(graph.vertices) == 1
    vertex = next(iter(graph.vertices.values()))
    expected_sig = signature_hash("json")
    assert vertex.object_signature == expected_sig
    assert vertex.object_key.endswith(f"/signature/{expected_sig}")
    assert vertex.argument_types == "json"


def test_parser_identity_merges_with_fresh_re_side(tmp_path):
    """The reconstructed key must be identity-equal to a fresh-RE key."""
    from db_project_manager.infrastructure.diff.comparator import identity_key
    from db_project_manager.infrastructure.parsing.pg_sql_parser import PgSqlParser

    _old_style_function_file(tmp_path, tmp_path)
    graph = PgSqlParser().parse_directory(tmp_path)
    vertex = next(iter(graph.vertices.values()))

    fresh_re_key = (
        f"pg_database/cis_zup_gp_dev/schema/cis_dmt_zup/type/function/"
        f"name/fun_etl/signature/{signature_hash('json')}"
    )
    assert identity_key(vertex.object_key) == identity_key(fresh_re_key)

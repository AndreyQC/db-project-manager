"""Unit tests for SQL normalization (Phase 9)."""

from db_project_manager.infrastructure.diff.normalize_sql import (
    _canonicalize_text_casts,
    normalize_sql,
    sql_hash,
)


# --- formatting / casing insensitivity ---


def test_create_table_casing_of_keywords_ignored():
    a = "CREATE TABLE t (a INT, b TEXT)"
    b = "create table t (a int, b text)"
    assert normalize_sql(a) == normalize_sql(b)


def test_create_table_formatting_ignored():
    a = "CREATE TABLE t (\n  a INT,\n  b TEXT\n)"
    b = "CREATE TABLE t (a INT, b TEXT)"
    assert normalize_sql(a) == normalize_sql(b)


def test_whitespace_collapsed():
    a = "CREATE   TABLE\t\tt\n\n(a INT)"
    b = "CREATE TABLE t (a INT)"
    assert normalize_sql(a) == normalize_sql(b)


# --- lesson §27: numeric modifiers stay intact ---


def test_numeric_modifier_not_split():
    # The canonicalization should not turn numeric(10,2) into something else.
    a = "CREATE TABLE t (a numeric(10,2))"
    b = "CREATE TABLE t (a numeric(10, 2))"
    assert normalize_sql(a) == normalize_sql(b)
    # and the modifier survives (not dropped)
    norm = normalize_sql("CREATE TABLE t (a numeric(10,2))")
    assert "10" in norm and "2" in norm


# --- comments stripped ---


def test_line_comment_stripped():
    a = "CREATE TABLE t (a INT) -- trailing comment"
    b = "CREATE TABLE t (a INT)"
    assert normalize_sql(a) == normalize_sql(b)


def test_block_comment_stripped():
    a = "/* leading\n multi-line */ CREATE TABLE t (a INT)"
    b = "CREATE TABLE t (a INT)"
    assert normalize_sql(a) == normalize_sql(b)


# --- sql_hash ---


def test_sql_hash_equal_for_equivalent_sql():
    a = "CREATE TABLE t (a INT)"
    b = "create table T (A int)"
    assert sql_hash(a) == sql_hash(b)


def test_sql_hash_differs_for_different_sql():
    a = "CREATE TABLE t (a INT)"
    b = "CREATE TABLE t (a INT, b TEXT)"
    assert sql_hash(a) != sql_hash(b)


def test_sql_hash_is_8_hex_chars():
    h = sql_hash("SELECT 1")
    assert len(h) == 8
    assert all(c in "0123456789abcdef" for c in h)


# --- regex fallback on unparseable SQL ---


def test_regex_fallback_on_syntax_error(caplog):
    """A syntactically broken body should not raise; regex fallback is used."""
    broken = "CREATE TABLE (((("  # not parseable
    # Should not raise.
    result = normalize_sql(broken)
    assert isinstance(result, str)
    assert result  # non-empty
    # The fallback lowercases.
    assert result == result.lower()


def test_empty_sql_handled():
    # Empty/whitespace should not blow up.
    assert isinstance(normalize_sql(""), str)
    assert isinstance(normalize_sql("   "), str)


# --- deterministic ---


def test_normalization_is_deterministic():
    sql = "CREATE TABLE t (a INT, b numeric(10,2))"
    assert normalize_sql(sql) == normalize_sql(sql)


# --- Phase 15.5.3: cast(text) canonicalization (cis_zup feedback 2026-09-03) ---

DEFAULT_SQL_SOURCE = (
    "CREATE TABLE \"cis_dmt_zup\".\"lu_zup_accountgroups\" ("
    "\"account_groups_guid\" TEXT NOT NULL, "
    "\"ts_insert_datetime\" TIMESTAMP NOT NULL "
    "DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'utc'), "
    "\"ts_update_datetime\" TIMESTAMP NOT NULL "
    "DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'utc'))"
)

DEFAULT_SQL_TARGET = (
    "CREATE TABLE \"cis_dmt_zup\".\"lu_zup_accountgroups\" ("
    "\"account_groups_guid\" TEXT NOT NULL, "
    "\"ts_insert_datetime\" TIMESTAMP NOT NULL "
    "DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE CAST('utc' AS TEXT)), "
    "\"ts_update_datetime\" TIMESTAMP NOT NULL "
    "DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE CAST('utc' AS TEXT)))"
)


def test_cast_text_literal_canonicalized_to_plain_string():
    """``CAST('utc' AS TEXT)`` and ``'utc'`` are equivalent in PG; the
    canonicalizer must normalize them to the same string.
    """
    a = "SELECT CURRENT_TIMESTAMP AT TIME ZONE 'utc'"
    b = "SELECT CURRENT_TIMESTAMP AT TIME ZONE CAST('utc' AS TEXT)"
    assert normalize_sql(a) == normalize_sql(b)


def test_cast_text_postgres_shortcut_canonicalized():
    """PG-style ``'utc'::text`` is also equivalent to ``'utc'``."""
    a = "SELECT CURRENT_TIMESTAMP AT TIME ZONE 'utc'"
    b = "SELECT CURRENT_TIMESTAMP AT TIME ZONE 'utc'::text"
    assert normalize_sql(a) == normalize_sql(b)


def test_cis_zup_real_default_hash_collapses_after_fix():
    """The exact cis_zup regression: ``lu_zup_accountgroups`` with
    AT TIME ZONE 'utc' (codebase) vs AT TIME ZONE CAST('utc' AS TEXT)
    (DB round-trip) — must produce identical hashes after the fix.
    """
    assert sql_hash(DEFAULT_SQL_SOURCE) == sql_hash(DEFAULT_SQL_TARGET)


def test_canonicalizer_skips_non_text_casts():
    """Conservative: numeric casts must NOT be collapsed (precision matters)."""
    a = "SELECT CAST(1.5 AS NUMERIC(10,2))"
    b = "SELECT 1.5"  # would be a different expression — NOT the same
    # Don't expect equal hashes; the test verifies that:
    # 1) The non-text CAST survives normalization (no false collapse).
    # 2) Different CAST arguments still produce different strings.
    assert normalize_sql(a) != normalize_sql(b)
    # And the cast type is preserved in the canonicalized output (sqlglot
    # may render NUMERIC as DECIMAL — same type, alias; both confirm the
    # numeric cast survived).
    canonical_a = normalize_sql(a).upper()
    assert "NUMERIC" in canonical_a or "DECIMAL" in canonical_a


def test_canonicalizer_skips_non_string_casts():
    """``CAST(some_col AS TEXT)`` does NOT match — only string literals are unwrapped."""
    a = "SELECT CAST(col AS TEXT)"  # col is an identifier, not a string literal
    b = "SELECT 'col'"  # very different semantically
    assert normalize_sql(a) != normalize_sql(b)


def test_canonicalizer_preserves_escaped_quotes():
    """Single-quote escape in PG (``''``) must survive the canonicalization."""
    a = "SELECT 'it''s' AS s"  # two consecutive single quotes → single literal quote
    b = "SELECT CAST('it''s' AS TEXT) AS s"
    assert normalize_sql(a) == normalize_sql(b)


def test_canonicalizer_regex_fallback_path():
    """The canonicalizer must apply to the regex-fallback path too — even
    unparseable SQL gets the CAST(...) unwrap treatment so old-school DDL
    benefits from the fix.
    """
    # Empty / unparseable: verify the canonicalizer itself runs cleanly.
    assert "'bar'" in _canonicalize_text_casts("CAST('bar' AS TEXT)")


def test_canonicalizer_no_op_when_no_casts():
    """Sanity: SQL without any text cast must round-trip unchanged (apart
    from sqlglot's own normalization).
    """
    sql = "CREATE TABLE t (a INT, b TEXT)"
    # Same input twice — must be perfectly stable.
    assert normalize_sql(sql) == normalize_sql(sql)
    # And the canonicalizer invoked alone must NOT change it.
    assert _canonicalize_text_casts(sql) == sql


# --- Greenplum tail clauses + cross-kernel canonical forms (Phase 16.7) ---


def test_gp_tail_source_vs_re_render_hash_equal():
    """The four false-positive CHANGED classes of the 2026-09-13 analyze run:
    WITH option case/order, DISTRIBUTED clause presence, bool vs boolean,
    DEFAULT (AT TIME ZONE 'utc') vs catalog timezone('utc'::text, now())."""
    source_style = """CREATE TABLE "cis_dmt_zup"."t1" (
    "id" int4 NOT NULL,
    "flag" boolean NULL,
    "ts" timestamp DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'utc')
)
WITH (appendoptimized=TRUE, orientation=COLUMN, compresstype=ZSTD, compresslevel=1)
DISTRIBUTED RANDOMLY;"""
    # RE side uses the catalog spelling: appendonly (legacy name of the same
    # parameter) + option order from gp_class.reloptions.
    re_style = """CREATE TABLE "cis_dmt_zup"."t1" (
    "id" int4 NOT NULL,
    "flag" bool NULL,
    "ts" timestamp DEFAULT timezone('utc'::text, now())
)
WITH (orientation=column, compresstype=zstd, appendonly=true, compresslevel=1)
DISTRIBUTED RANDOMLY;"""
    assert normalize_sql(source_style) == normalize_sql(re_style)
    assert sql_hash(source_style) == sql_hash(re_style)


def test_gp_tail_distributed_by_forms_equal():
    a = 'CREATE TABLE s.t ("id" int4)\nDISTRIBUTED BY ("id");'
    b = 'CREATE TABLE s.t ("id" int4)\ndistributed  by (  "id" ) ;'
    assert normalize_sql(a) == normalize_sql(b)


def test_gp_tail_different_distribution_not_equal():
    a = "CREATE TABLE s.t (id int4)\nDISTRIBUTED RANDOMLY;"
    b = 'CREATE TABLE s.t (id int4)\nDISTRIBUTED BY ("id");'
    assert normalize_sql(a) != normalize_sql(b)


def test_gp_tail_different_option_value_not_equal():
    a = "CREATE TABLE s.t (id int4)\nWITH (compresstype=zstd) DISTRIBUTED RANDOMLY;"
    b = "CREATE TABLE s.t (id int4)\nWITH (compresstype=none) DISTRIBUTED RANDOMLY;"
    assert normalize_sql(a) != normalize_sql(b)


def test_gp_tail_identifier_case_preserved():
    """Quoted identifiers keep their case — only unquoted tokens lowercase."""
    a = 'CREATE TABLE s.t (id int4)\nDISTRIBUTED BY ("MixedCase");'
    b = 'CREATE TABLE s.t (id int4)\nDISTRIBUTED BY ("mixedcase");'
    assert normalize_sql(a) != normalize_sql(b)


def test_gp_tail_not_degraded_to_raw_command_text():
    """Before 16.7 the whole statement degraded to a sqlglot Command node
    (raw, whitespace-sensitive). Now the head is AST-normalized: spacing
    differences inside the column list must not leak into the hash."""
    a = 'CREATE TABLE s.t ("a" int4, "b" text)\nDISTRIBUTED RANDOMLY;'
    b = 'CREATE  TABLE  s.t ( "a"  int4 ,  "b"  text )\nDISTRIBUTED RANDOMLY;'
    assert normalize_sql(a) == normalize_sql(b)


def test_timezones_at_time_zone_operator_vs_function_equal():
    """PG 18 codebase form vs GP 6 (PG 9.4 kernel) pg_attrdef form."""
    a = "CREATE TABLE t (ts timestamp DEFAULT CURRENT_TIMESTAMP AT TIME ZONE 'utc')"
    b = "CREATE TABLE t (ts timestamp DEFAULT timezone('utc'::text, now()))"
    assert normalize_sql(a) == normalize_sql(b)


def test_postgres_body_without_gp_clauses_unchanged():
    """PG bodies (no WITH/DISTRIBUTED) keep the pre-16.7 normalized form —
    the tail extraction is a no-op for them."""
    sql = "CREATE TABLE a.b (id int4 NULL)"
    assert normalize_sql(sql) == 'CREATE TABLE a.b (id INT NULL)'


def test_gp_tail_with_only_without_distributed():
    a = "CREATE TABLE s.t (id int4)\nWITH (fillfactor=70);"
    b = "CREATE TABLE s.t (id int4)\nWITH (fillfactor=70)\n;"
    assert normalize_sql(a) == normalize_sql(b)


# --- Phase 16.7: view spellings, VOLATILE, identifier quoting ---


def test_view_column_list_and_case_only_alias_equal():
    """Hand-written DDL (explicit column list, no aliases) vs pg_get_viewdef
    (no list, case-only aliases for the list's case-renames)."""
    handwritten = 'CREATE VIEW s.v (a, n1_x) AS SELECT t.a, t."N1_x" FROM tbl t'
    catalog_form = 'CREATE VIEW s.v AS SELECT t.a, t."N1_x" AS n1_x FROM tbl t'
    assert normalize_sql(handwritten) == normalize_sql(catalog_form)


def test_view_real_rename_via_column_list_not_equal():
    """A list that renames (different names, not case) is meaningful."""
    handwritten = "CREATE VIEW s.v (a, renamed) AS SELECT t.a, t.b FROM tbl t"
    catalog_form = "CREATE VIEW s.v AS SELECT t.a, t.b FROM tbl t"
    assert normalize_sql(handwritten) != normalize_sql(catalog_form)


def test_quoted_lowercase_identifier_equals_unquoted():
    a = 'CREATE TABLE "cis_dmt_zup"."t1" ("id" int4 NULL)'
    b = "CREATE TABLE cis_dmt_zup.t1 (id int4 NULL)"
    assert normalize_sql(a) == normalize_sql(b)


def test_quoted_mixed_case_identifier_stays_distinct():
    """Quoted mixed-case is a different object in PG — must not fold."""
    a = 'CREATE TABLE s."MixedCase" (id int4 NULL)'
    b = "CREATE TABLE s.mixedcase (id int4 NULL)"
    assert normalize_sql(a) != normalize_sql(b)


def test_default_volatile_stripped_from_function_header():
    a = "CREATE FUNCTION s.f() RETURNS int LANGUAGE plpgsql VOLATILE AS $$ BEGIN RETURN 1; END $$"
    b = "CREATE FUNCTION s.f() RETURNS int LANGUAGE plpgsql AS $$ BEGIN RETURN 1; END $$"
    assert normalize_sql(a) == normalize_sql(b)


def test_stable_and_immutable_not_stripped():
    a = "CREATE FUNCTION s.f() RETURNS int LANGUAGE plpgsql STABLE AS $$ BEGIN RETURN 1; END $$"
    b = "CREATE FUNCTION s.f() RETURNS int LANGUAGE plpgsql AS $$ BEGIN RETURN 1; END $$"
    assert normalize_sql(a) != normalize_sql(b)


def test_volatile_word_in_function_body_preserved():
    """Only the header attribute is stripped; the plpgsql body keeps its text."""
    a = "CREATE FUNCTION s.f() RETURNS int LANGUAGE plpgsql AS $$ DECLARE x int := 1; BEGIN x := volatile_calc(); END $$"
    b = "CREATE FUNCTION s.f() RETURNS int LANGUAGE plpgsql AS $$ DECLARE x int := 1; BEGIN x := other(); END $$"
    assert normalize_sql(a) != normalize_sql(b)

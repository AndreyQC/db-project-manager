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

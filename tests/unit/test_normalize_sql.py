"""Unit tests for SQL normalization (Phase 9)."""

from db_project_manager.infrastructure.diff.normalize_sql import normalize_sql, sql_hash


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

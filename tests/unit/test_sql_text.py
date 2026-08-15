"""Unit tests for infrastructure.sql.sql_text (BACKLOG P1).

Contract: has_executable_sql must never return False for text that contains
executable SQL (a false 'empty' would silently skip a deploy statement),
while comment-only bodies (real case: database_setting without explicit
db-level settings) are reported as empty.
"""

from db_project_manager.infrastructure.sql.sql_text import has_executable_sql, strip_sql_comments

# --- has_executable_sql: empty / comments-only ---


def test_empty_and_whitespace_only() -> None:
    assert has_executable_sql("") is False
    assert has_executable_sql("   \n\t  \n") is False


def test_line_comments_only() -> None:
    # Body of a real RE output for database_setting on a clean PG.
    body = (
        "-- Параметры уровня базы (ALTER DATABASE ... SET).\n"
        "-- Имя БД — исходное (из object_catalog); deploy-сервис подменяет его\n"
        "-- на имя целевой/временной БД перед выполнением (P5.S07).\n"
    )
    assert has_executable_sql(body) is False


def test_block_comments_only() -> None:
    assert has_executable_sql("/* автодoc-заголовок\nмногострочный */") is False
    assert has_executable_sql("-- x\n/* y */\n") is False


def test_statement_with_comments_is_executable() -> None:
    text = (
        "-- comment\n"
        "/* block */\n"
        'ALTER DATABASE "demo" SET work_mem = \'64MB\';\n'
    )
    assert has_executable_sql(text) is True


# --- quote awareness: '--' inside literals is NOT a comment ---


def test_dash_dash_inside_string_literal() -> None:
    assert has_executable_sql("SELECT '-- not a comment';") is True
    # The whole statement hidden behind a literal must still count.
    assert has_executable_sql("SELECT 'a -- b' -- real comment\n") is True


def test_dash_dash_inside_quoted_identifier() -> None:
    assert has_executable_sql('SELECT "col--name" FROM t;') is True


def test_block_comment_marker_inside_literal() -> None:
    assert has_executable_sql("SELECT '/* not a comment */';") is True


def test_dollar_quoted_body_with_comment_like_text() -> None:
    sql = (
        "CREATE FUNCTION f() RETURNS void AS $$\n"
        "  -- comment inside body\n"
        "  SELECT 1;\n"
        "$$ LANGUAGE plpgsql;"
    )
    assert has_executable_sql(sql) is True
    # The dollar-quoted string itself is executable content even if it only
    # holds comment-like text.
    assert has_executable_sql("SELECT $$--$$;") is True


def test_dollar_tag_with_name() -> None:
    assert has_executable_sql("SELECT $tag_1$--$$tag_1$;") is True


# --- strip_sql_comments output ---


def test_strip_replaces_comment_with_space_not_empty_between_tokens() -> None:
    assert strip_sql_comments("SELECT 1--c\nFROM t") == "SELECT 1 \nFROM t"
    assert strip_sql_comments("SELECT/*c*/1") == "SELECT 1"


def test_strip_preserves_literals_verbatim() -> None:
    assert strip_sql_comments("SELECT 'a--b'") == "SELECT 'a--b'"
    assert strip_sql_comments("SELECT 'it''s -- fine'") == "SELECT 'it''s -- fine'"


def test_strip_unterminated_literal_kept_to_end() -> None:
    # Conservative: never drop potentially executable content.
    assert strip_sql_comments("SELECT 'oops") == "SELECT 'oops"

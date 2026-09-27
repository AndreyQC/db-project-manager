"""Tests for the SQL classifier (Phase 19, MCP-4)."""

from __future__ import annotations

import pytest

from db_project_manager.infrastructure.sql.classify import (
    StatementClass,
    classify_script,
    dialect_for,
)

D = "postgres"


def verdict(sql: str, dialect: str = D) -> StatementClass:
    return classify_script(sql, dialect=dialect).worst


# --- read-only statements ---

@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "SELECT * FROM public.bookings WHERE id = 1",
        "WITH c AS (SELECT 1) SELECT * FROM c",
        "(SELECT 1)",
        "SELECT 1 UNION SELECT 2",
        "SELECT 1 EXCEPT SELECT 2",
        "SELECT 1 INTERSECT SELECT 2",
        "VALUES (1), (2)",
        "SHOW statement_timeout",
        "EXPLAIN SELECT * FROM t",
        "EXPLAIN ANALYZE SELECT * FROM t",
        "-- comment\nSELECT 1;  -- trailing comment",
    ],
)
def test_read_only(sql):
    v = classify_script(sql, dialect=D)
    assert v.is_read_only, v.summary()
    assert v.worst is StatementClass.READ_ONLY


# --- write statements ---

@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO t VALUES (1)",
        "UPDATE t SET x = 1",
        "DELETE FROM t",
        "MERGE INTO t USING s ON t.id = s.id WHEN MATCHED THEN UPDATE SET x = 1",
        "CREATE TABLE t (x int)",
        "ALTER TABLE t ADD COLUMN y int",
        "COMMENT ON TABLE t IS 'x'",
        "ANALYZE t",
        "COPY t FROM 'f.csv'",
        "SET search_path TO public",
        "BEGIN",
        "COMMIT",
        "ROLLBACK",
        "VACUUM t",
        "CALL public.p()",
        "DO $$ BEGIN PERFORM 1; END $$",
        "GRANT SELECT ON t TO u",
        "REVOKE SELECT ON t FROM u",
        # escape attempts: read-only transaction breakers
        "SELECT 1; COMMIT",
        "ROLLBACK; SELECT 1",
        "SELECT * FROM t FOR UPDATE",
        "SELECT * FROM t FOR SHARE",
    ],
)
def test_write(sql):
    v = classify_script(sql, dialect=D)
    assert verdict(sql) is StatementClass.WRITE, v.summary()
    assert not v.is_read_only
    assert v.requires_write_permission


def test_read_only_with_locking_clause_is_write():
    v = classify_script("SELECT * FROM public.t WHERE id = 1 FOR UPDATE", dialect=D)
    assert v.worst is StatementClass.WRITE
    assert "FOR UPDATE" in v.summary()


# --- denylisted functions inside read-only statements ---

@pytest.mark.parametrize(
    "fn",
    ["dblink", "dblink_exec", "pg_read_file", "pg_sleep", "lo_import", "lo_export", "nextval", "setval"],
)
def test_read_only_denylisted_function(fn):
    sql = f"SELECT {fn}('x')"
    v = classify_script(sql, dialect=D)
    assert v.worst is StatementClass.WRITE
    assert fn in v.summary()


def test_qualified_denylisted_function():
    v = classify_script("SELECT pg_catalog.pg_read_file('/etc/passwd')", dialect=D)
    assert v.worst is StatementClass.WRITE


def test_schema_qualified_nextval():
    v = classify_script("SELECT public.nextval('public.s1')", dialect=D)
    assert v.worst is StatementClass.WRITE


def test_allowed_sequence_read():
    assert verdict("SELECT currval('s1')") is StatementClass.READ_ONLY


# --- destructive statements ---

@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE t",
        "DROP SCHEMA s CASCADE",
        "DROP FUNCTION f()",
        "DROP INDEX ix",
        "DROP VIEW v",
        "TRUNCATE TABLE t",
        "TRUNCATE t",
    ],
)
def test_destructive(sql):
    v = classify_script(sql, dialect=D)
    assert v.worst is StatementClass.DESTRUCTIVE, v.summary()
    assert v.requires_destructive_confirm


# --- unknown / fail-safe ---

def test_unparseable_is_unknown():
    v = classify_script("CREATE OR REPLACE @@garbage", dialect=D)
    assert v.worst is StatementClass.UNKNOWN
    assert v.requires_destructive_confirm  # fail-safe: unknown == destructive


def test_unclassified_command_is_unknown():
    v = classify_script("LISTEN channel", dialect=D)
    assert v.worst is StatementClass.UNKNOWN


def test_empty_script_is_unknown():
    assert classify_script("", dialect=D).worst is StatementClass.UNKNOWN
    assert classify_script("-- только комментарий\n", dialect=D).worst is StatementClass.UNKNOWN


# --- multi-statement aggregation ---

def test_multi_statement_worst_wins():
    v = classify_script("SELECT 1; INSERT INTO t VALUES (1); DROP TABLE t", dialect=D)
    assert v.worst is StatementClass.DESTRUCTIVE
    assert len(v.statements) == 3
    assert not v.is_read_only


def test_multi_statement_read_only():
    assert classify_script("SELECT 1; SELECT 2", dialect=D).is_read_only


def test_summary_lists_every_statement():
    v = classify_script("SELECT 1; DROP TABLE t", dialect=D)
    assert "read_only" in v.summary()
    assert "destructive" in v.summary()


# --- dialects ---

def test_dialect_map():
    assert dialect_for("postgres") == "postgres"
    assert dialect_for("Greenplum") == "postgres"
    assert dialect_for("mssql") == "tsql"
    assert dialect_for("unknown_engine") == "postgres"


def test_tsql_dialect_parses_top():
    assert verdict("SELECT TOP 5 * FROM t", dialect="tsql") is StatementClass.READ_ONLY

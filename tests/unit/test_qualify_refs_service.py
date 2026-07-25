"""Tests for QualifyRefsService — bare reference qualification.

Uses a temporary codebase with intentional bare references:
  - function sp_caller() calls sp_helper() bare → should be qualified
  - function sp_helper exists in schema 'app' (unique name)
  - view v_users references table users in FROM → should be qualified
  - table 'ambiguous_name' exists in BOTH 'public' and 'app' → skipped + logged
  - reserved keyword 'count' should NOT be qualified
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from db_project_manager.application.qualify_refs_service import (
    QualifyRefsService,
    REPORT_FILE,
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dedent(content).lstrip(), encoding="utf-8")


_AUTODOC = """\
/*====================================================================================
[<[autodoc-yaml]]
object:
  object_catalog: testdb
  object_schema: {schema}
  object_type: {otype}
  object_name: {name}
  object_key: pg_database/testdb/schema/{schema}/type/{otype}/name/{name}
project:
  build: true
[[autodoc-yaml]>]
=====================================================================================*/

"""


def _make_file(path: Path, schema: str, otype: str, name: str, body: str) -> None:
    header = _AUTODOC.format(schema=schema, otype=otype, name=name)
    _write(path, header + body)


@pytest.fixture
def codebase(tmp_path: Path) -> Path:
    """A codebase with intentional bare references to qualify."""
    root = tmp_path / "testdb"

    # Function sp_helper in schema 'app' — referenced bare by sp_caller.
    _make_file(
        root / "app" / "functions" / "function sp_helper.sql",
        "app", "function", "sp_helper",
        """
        CREATE OR REPLACE FUNCTION app.sp_helper(x int) RETURNS int
        LANGUAGE sql AS $$ SELECT x * 2 $$;
        """,
    )

    # Function sp_caller in schema 'app' — calls sp_helper() BARE.
    _make_file(
        root / "app" / "functions" / "function sp_caller.sql",
        "app", "function", "sp_caller",
        """
        CREATE OR REPLACE FUNCTION app.sp_caller(x int) RETURNS int
        LANGUAGE sql AS $$
            SELECT sp_helper(x);
        $$;
        """,
    )

    # Table 'users' in schema 'app' — referenced bare by view.
    _make_file(
        root / "app" / "tables" / "table users.sql",
        "app", "table", "users",
        'CREATE TABLE app.users (id int, name text);\n',
    )

    # View v_users references 'users' BARE in FROM.
    _make_file(
        root / "app" / "views" / "view v_users.sql",
        "app", "view", "v_users",
        """
        CREATE OR REPLACE VIEW app.v_users AS
            SELECT id, name FROM users;
        """,
    )

    # Ambiguous: 'audit' table in BOTH 'public' and 'app'.
    _make_file(
        root / "public" / "tables" / "table audit.sql",
        "public", "table", "audit",
        'CREATE TABLE public.audit (id int);\n',
    )
    _make_file(
        root / "app" / "tables" / "table audit.sql",
        "app", "table", "audit",
        'CREATE TABLE app.audit (id int);\n',
    )

    # Function that references the ambiguous 'audit' bare — should be SKIPPED.
    _make_file(
        root / "app" / "functions" / "function sp_uses_audit.sql",
        "app", "function", "sp_uses_audit",
        """
        CREATE OR REPLACE FUNCTION app.sp_uses_audit() RETURNS int
        LANGUAGE sql AS $$ SELECT count(*)::int FROM audit $$;
        """,
    )

    # Function calling itself (self-ref) — should NOT be qualified.
    _make_file(
        root / "app" / "functions" / "function sp_recursive.sql",
        "app", "function", "sp_recursive",
        """
        CREATE OR REPLACE FUNCTION app.sp_recursive(n int) RETURNS int
        LANGUAGE sql AS $$
            SELECT CASE WHEN n <= 1 THEN 1 ELSE sp_recursive(n - 1) END;
        $$;
        """,
    )

    return root


# --- core behaviour ---


def test_qualifies_bare_function_call(codebase: Path) -> None:
    """sp_caller's body calls sp_helper() bare → becomes app.sp_helper()."""
    svc = QualifyRefsService()
    svc.run(codebase)
    text = (codebase / "app" / "functions" / "function sp_caller.sql").read_text(encoding="utf-8")
    assert "app.sp_helper(" in text
    assert " sp_helper(" not in text  # no bare call left


def test_qualifies_bare_table_in_from(codebase: Path) -> None:
    """v_users FROM users → FROM app.users."""
    svc = QualifyRefsService()
    svc.run(codebase)
    text = (codebase / "app" / "views" / "view v_users.sql").read_text(encoding="utf-8")
    assert "FROM app.users" in text
    assert "FROM users" not in text


def test_skips_self_reference(codebase: Path) -> None:
    """sp_recursive calling itself must NOT be qualified."""
    svc = QualifyRefsService()
    svc.run(codebase)
    text = (codebase / "app" / "functions" / "function sp_recursive.sql").read_text(encoding="utf-8")
    # Body preserved — sp_recursive() stays bare.
    assert "sp_recursive(n - 1)" in text


def test_skips_reserved_keyword_count(codebase: Path) -> None:
    """'count(' is a built-in aggregate, not a user object → skip."""
    svc = QualifyRefsService()
    svc.run(codebase)
    text = (codebase / "app" / "functions" / "function sp_uses_audit.sql").read_text(encoding="utf-8")
    # 'count' must NOT have a schema prefix added.
    assert "app.count(" not in text
    assert "count(*)" in text


def test_skips_ambiguous_name(codebase: Path) -> None:
    """'audit' exists in both public and app → skip + tracked."""
    svc = QualifyRefsService()
    report = svc.run(codebase)
    assert "audit" in report.ambiguous
    text = (codebase / "app" / "functions" / "function sp_uses_audit.sql").read_text(encoding="utf-8")
    # Body preserved — 'audit' stays bare (user must resolve manually).
    assert "FROM audit" in text


def test_dry_run_does_not_modify_files(codebase: Path) -> None:
    """--dry-run: scan + report, but files unchanged."""
    svc = QualifyRefsService()
    before = (codebase / "app" / "functions" / "function sp_caller.sql").read_text(encoding="utf-8")
    svc.run(codebase, dry_run=True)
    after = (codebase / "app" / "functions" / "function sp_caller.sql").read_text(encoding="utf-8")
    assert before == after


def test_autodoc_records_qualify_report(codebase: Path) -> None:
    """Modified file's autodoc gets qualify_report: [...] field."""
    svc = QualifyRefsService()
    svc.run(codebase)
    from db_project_manager.infrastructure.sql.autodoc import extract_header

    text = (codebase / "app" / "functions" / "function sp_caller.sql").read_text(encoding="utf-8")
    parsed = extract_header(text)
    assert parsed is not None
    assert "qualify_report" in parsed
    assert "sp_helper" in parsed["qualify_report"]


def test_report_file_written(codebase: Path) -> None:
    """_qualify_report.md is created at codebase root."""
    svc = QualifyRefsService()
    svc.run(codebase)
    report_path = codebase / REPORT_FILE
    assert report_path.is_file()
    content = report_path.read_text(encoding="utf-8")
    assert "Qualify Refs Report" in content
    assert "sp_helper" in content  # listed in qualified section
    assert "audit" in content      # listed in ambiguous section


def test_empty_codebase(tmp_path: Path) -> None:
    """Codebase with no SQL files produces a minimal report, no crash."""
    root = tmp_path / "empty"
    root.mkdir()
    svc = QualifyRefsService()
    report = svc.run(root)
    assert report.files_scanned == 0
    assert report.changes == []
    assert (root / REPORT_FILE).is_file()


def test_nonexistent_codebase_raises(tmp_path: Path) -> None:
    from db_project_manager.application.qualify_refs_service import QualifyRefsError

    svc = QualifyRefsService()
    with pytest.raises(QualifyRefsError, match="not found"):
        svc.run(tmp_path / "does_not_exist")

"""Routine DROP identities always carry the argument list (Phase 18 hotfix).

Kernels < PG 10 (Greenplum 6 = kernel 9.4) make the argument list mandatory
in DROP FUNCTION/PROCEDURE/AGGREGATE grammar: ``DROP FUNCTION name;`` fails
with a syntax error at the token right AFTER the name, ``DROP FUNCTION
name();`` is valid. ``pg_get_function_identity_arguments`` returns '' for
zero-arg routines, and the adapter used to omit the parens then. Live
failure on cis_zup_gp_dev (2026-09-24): the error first pointed at
``CASCADE`` (misread as unsupported), then — with CASCADE removed — at
``;``, exposing the missing parens as the real cause. CASCADE is legal on
every kernel once the parens are in place.
"""

from __future__ import annotations

from db_project_manager.infrastructure.database.postgres import queries as q
from db_project_manager.infrastructure.database.postgres.adapter import PGDatabaseAdapter


class _FakeConnection:
    """Records executed DDL statements (drop_schema_contents drops go here)."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement) -> None:
        self.statements.append(str(statement))


def _make_adapter(
    monkeypatch, *, prokind_supported: bool
) -> tuple[PGDatabaseAdapter, _FakeConnection]:
    adapter = PGDatabaseAdapter()
    adapter._is_greenplum = not prokind_supported  # GP 6 kernel has no prokind
    connection = _FakeConnection()
    adapter._connection = connection

    def fake_exec(query: str, params: dict | None = None) -> list:
        if query == q.PROKIND_PROBE and not prokind_supported:
            raise RuntimeError("column p.prokind does not exist")
        if query == q.GET_SCHEMA_DROPPABLE_OBJECTS:
            return [("tbl_a", "r")]
        if query in (q.GET_SCHEMA_ROUTINES_POSTGRES, q.GET_SCHEMA_ROUTINES_GREENPLUM):
            return [
                ("fn_zero", "", "f"),
                ("fn_arg", "integer", "f"),
                ("pr_zero", "", "p"),
                ("agg_a", "integer", "a"),
            ]
        return []

    monkeypatch.setattr(adapter, "_exec", fake_exec)
    return adapter, connection


def test_zero_arg_routines_drop_with_empty_parens(monkeypatch) -> None:
    """The live-failure statement shape: mandatory empty argument list."""
    adapter, connection = _make_adapter(monkeypatch, prokind_supported=False)

    adapter.drop_schema_contents("cis_dmt_zup")

    statements = connection.statements
    assert 'DROP FUNCTION IF EXISTS "cis_dmt_zup"."fn_zero"() CASCADE;' in statements
    assert 'DROP PROCEDURE IF EXISTS "cis_dmt_zup"."pr_zero"() CASCADE;' in statements
    assert not any(s.endswith('"fn_zero";') for s in statements)


def test_routines_with_args_keep_identity_list(monkeypatch) -> None:
    adapter, connection = _make_adapter(monkeypatch, prokind_supported=True)

    adapter.drop_schema_contents("app")

    statements = connection.statements
    assert 'DROP FUNCTION IF EXISTS "app"."fn_arg"(integer) CASCADE;' in statements
    assert 'DROP AGGREGATE IF EXISTS "app"."agg_a"(integer) CASCADE;' in statements


def test_relational_objects_keep_cascade(monkeypatch) -> None:
    adapter, connection = _make_adapter(monkeypatch, prokind_supported=False)

    adapter.drop_schema_contents("app")

    assert 'DROP TABLE IF EXISTS "app"."tbl_a" CASCADE;' in connection.statements

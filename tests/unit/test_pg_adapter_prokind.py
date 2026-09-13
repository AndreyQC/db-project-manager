"""pg_proc.prokind capability probe for functions/procedures (Phase 16.4).

Greenplum 6 (kernel PG 9.4) has no ``prokind`` (PG 11+) and no procedures at
all; it also still has ``proisagg``/``proiswindow`` (removed in PG 11), so the
legacy function query is a separate query pair, selected by a per-connection
probe — probed once, not per schema.
"""

from __future__ import annotations

import pytest

from db_project_manager.infrastructure.database.postgres import queries as q
from db_project_manager.infrastructure.database.postgres.adapter import PGDatabaseAdapter

_FUNCTION_ROW = tuple(["fn_x", "public"] + ["t"] * 6 + [None])
_PROCEDURE_ROW = tuple(["pr_x", "public"] + ["t"] * 5 + [None])


def _make_adapter(
    monkeypatch, *, is_greenplum: bool, fail_prokind: bool
) -> tuple[PGDatabaseAdapter, list[str]]:
    adapter = PGDatabaseAdapter()
    adapter._is_greenplum = is_greenplum
    calls: list[str] = []

    def fake_exec(query: str, params: dict | None = None) -> list:
        calls.append(query)
        if fail_prokind and query == q.PROKIND_PROBE:
            raise RuntimeError('column p.prokind does not exist')
        if query in (q.GET_FUNCTIONS_POSTGRES, q.GET_FUNCTIONS_GREENPLUM):
            return [_FUNCTION_ROW]
        if query == q.GET_PROCEDURES_POSTGRES:
            return [_PROCEDURE_ROW]
        return []

    monkeypatch.setattr(adapter, "_exec", fake_exec)
    return adapter, calls


def test_gp6_functions_use_legacy_query_and_probe_once(monkeypatch) -> None:
    adapter, calls = _make_adapter(monkeypatch, is_greenplum=True, fail_prokind=True)

    first = adapter._get_functions("schema_a")
    adapter._get_functions("schema_b")

    assert first and first[0]["name"] == "fn_x"
    assert calls.count(q.PROKIND_PROBE) == 1  # probe once, not per schema
    assert calls.count(q.GET_FUNCTIONS_GREENPLUM) == 2
    assert calls.count(q.GET_FUNCTIONS_POSTGRES) == 0


def test_gp6_procedures_return_empty_without_query(monkeypatch) -> None:
    adapter, calls = _make_adapter(monkeypatch, is_greenplum=True, fail_prokind=True)

    assert adapter._get_procedures("schema_a") == []
    assert q.GET_PROCEDURES_POSTGRES not in calls


def test_gp7_keeps_prokind_query(monkeypatch) -> None:
    """A greenplum-typed connection to GP 7 (prokind present) must keep the
    PG 11+ query — the probe wins over a type-based branch."""
    adapter, calls = _make_adapter(monkeypatch, is_greenplum=True, fail_prokind=False)

    adapter._get_functions("schema_a")  # routed by the cached probe result
    procedures = adapter._get_procedures("schema_a")

    assert calls.count(q.GET_FUNCTIONS_POSTGRES) == 1
    assert calls.count(q.GET_FUNCTIONS_GREENPLUM) == 0
    assert procedures and procedures[0]["name"] == "pr_x"


def test_postgres_connection_prokind_failure_still_raises(monkeypatch) -> None:
    adapter, _calls = _make_adapter(monkeypatch, is_greenplum=False, fail_prokind=True)

    with pytest.raises(RuntimeError, match="prokind"):
        adapter._get_functions("schema_a")

"""Greenplum sequence fallback: one capability probe per connection (Phase 16.3).

Regression for the noisy RE log on GP 6: every schema extraction ran the doomed
``pg_sequence`` query first and logged a WARNING with the full SQL text. The
probe must happen once per connection (and at INFO — the condition is expected
on GP < PG 10), with the decision cached afterwards.
"""

from __future__ import annotations

import pytest

from db_project_manager.infrastructure.database.postgres import queries as q
from db_project_manager.infrastructure.database.postgres.adapter import PGDatabaseAdapter

#: One minimal 13-column row accepted by _get_sequences parsing.
_FALLBACK_ROW = tuple(["seq_x", "public"] + [None] * 11)


def _make_adapter(monkeypatch, *, is_greenplum: bool, fail_pg_sequence: bool) -> tuple[PGDatabaseAdapter, list[str]]:
    adapter = PGDatabaseAdapter()
    adapter._is_greenplum = is_greenplum
    calls: list[str] = []

    def fake_exec(query: str, params: dict | None = None) -> list:
        calls.append(query)
        if fail_pg_sequence and query == q.GET_SEQUENCES_POSTGRES:
            raise RuntimeError('relation "pg_sequence" does not exist')
        return [ _FALLBACK_ROW ]

    monkeypatch.setattr(adapter, "_exec", fake_exec)
    return adapter, calls


def test_gp_fallback_probes_pg_sequence_once_per_connection(monkeypatch) -> None:
    adapter, calls = _make_adapter(monkeypatch, is_greenplum=True, fail_pg_sequence=True)

    first = adapter._get_sequences("schema_a")
    second = adapter._get_sequences("schema_b")

    assert first and second  # fallback rows parsed both times
    assert calls.count(q.GET_SEQUENCES_POSTGRES) == 1  # probe once, not per schema
    assert calls.count(q.GET_SEQUENCES_GREENPLUM) == 2


def test_gp_fallback_logged_once_at_info_never_warning(monkeypatch) -> None:
    from loguru import logger

    adapter, _calls = _make_adapter(monkeypatch, is_greenplum=True, fail_pg_sequence=True)
    messages: list[str] = []
    sink_id = logger.add(messages.append, level="DEBUG")
    try:
        adapter._get_sequences("schema_a")
        adapter._get_sequences("schema_b")
    finally:
        logger.remove(sink_id)

    pg_sequence_msgs = [m for m in messages if "pg_sequence" in m]
    assert len(pg_sequence_msgs) == 1  # once per connection, not per schema
    assert "WARNING" not in pg_sequence_msgs[0]  # expected condition, not a problem


def test_gp7_with_pg_sequence_keeps_richer_query(monkeypatch) -> None:
    """A greenplum-typed connection to GP 7 (pg_sequence present) must keep
    using the richer PG query — the probe wins over a type-based branch."""
    adapter, calls = _make_adapter(monkeypatch, is_greenplum=True, fail_pg_sequence=False)

    adapter._get_sequences("schema_a")
    adapter._get_sequences("schema_b")

    assert calls.count(q.GET_SEQUENCES_POSTGRES) == 2
    assert calls.count(q.GET_SEQUENCES_GREENPLUM) == 0


def test_postgres_connection_pg_sequence_error_still_raises(monkeypatch) -> None:
    adapter, _calls = _make_adapter(monkeypatch, is_greenplum=False, fail_pg_sequence=True)

    with pytest.raises(RuntimeError, match="pg_sequence"):
        adapter._get_sequences("schema_a")

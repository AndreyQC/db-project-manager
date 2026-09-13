"""Greenplum admin schemas are excluded from reverse-engineering (Phase 16.5).

Live-probe facts behind the fix (cis_zup_gp_dev, GP 6.19.4): ``gp_toolkit`` is
the only non-``pg_%`` admin schema; it is NOT extension-owned (0 of 52 objects
carry ``pg_depend.deptype='e'``, ``pg_extension`` holds only plpgsql), so the
schema list is the only filter point. Exclusion is greenplum-only: on a
PostgreSQL connection a same-name schema is a user schema.
"""

from __future__ import annotations

from db_project_manager.infrastructure.database.postgres import queries as q
from db_project_manager.infrastructure.database.postgres.adapter import GP_ADMIN_SCHEMAS, PGDatabaseAdapter

_ROWS = [
    ("cis_dmt_zup", None),
    ("gp_toolkit", None),
    ("public", None),
    ("src_ods_hn_zup", None),
]


def _make_adapter(monkeypatch, *, is_greenplum: bool) -> PGDatabaseAdapter:
    adapter = PGDatabaseAdapter()
    adapter._is_greenplum = is_greenplum
    monkeypatch.setattr(adapter, "_exec", lambda query, params=None: list(_ROWS) if query == q.GET_SCHEMAS else [])
    return adapter


def test_greenplum_admin_schemas_excluded(monkeypatch) -> None:
    adapter = _make_adapter(monkeypatch, is_greenplum=True)

    names = [s["name"] for s in adapter._get_schemas()]

    assert "gp_toolkit" not in names
    assert names == ["cis_dmt_zup", "public", "src_ods_hn_zup"]


def test_postgres_connection_keeps_same_name_user_schema(monkeypatch) -> None:
    adapter = _make_adapter(monkeypatch, is_greenplum=False)

    names = [s["name"] for s in adapter._get_schemas()]

    assert names == ["cis_dmt_zup", "gp_toolkit", "public", "src_ods_hn_zup"]


def test_exclusion_logged_once_at_info(monkeypatch) -> None:
    from loguru import logger

    adapter = _make_adapter(monkeypatch, is_greenplum=True)
    messages: list[str] = []
    sink_id = logger.add(messages.append, level="DEBUG")
    try:
        adapter._get_schemas()
    finally:
        logger.remove(sink_id)

    exclusion_logs = [m for m in messages if "gp_toolkit" in str(m)]
    assert len(exclusion_logs) == 1
    assert any("Админ-схемы GP исключены" in str(m) for m in exclusion_logs)


def test_gp7_statistics_schemas_listed_upfront() -> None:
    """gp_statistics*/ exist on GP 7 only; naming them now keeps the filter
    forward-compatible (a no-op on GP 6 where the schemas don't exist)."""
    assert "gp_statistics" in GP_ADMIN_SCHEMAS
    assert "gp_statistics_history" in GP_ADMIN_SCHEMAS

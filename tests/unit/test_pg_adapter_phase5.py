"""Tests for Phase 5 adapter readers: extensions and database settings.

_exec is stubbed per query text — no live DB needed. The shape guards the
contract consumed by SQLGenerator (P5.S03) and deploy (P5.S07).
"""

from __future__ import annotations

from typing import Any

import pytest

from db_project_manager.infrastructure.database.postgres import queries as q
from db_project_manager.infrastructure.database.postgres.adapter import PGDatabaseAdapter

_EXTENSIONS_ROWS = [
    ("citext", "public", "1.6", "case-insensitive text"),
    ("uuid-ossp", "extensions", "1.1", None),
]
_PROPERTIES_ROW = [("UTF8", "ru_RU.UTF-8", "ru_RU.UTF-8")]
_SETTINGS_ROWS = [("work_mem=64MB",), ("statement_timeout=30s",)]


def _adapter_stubbed(rows_by_query: dict[str, list]) -> PGDatabaseAdapter:
    adapter = PGDatabaseAdapter()
    adapter._connection = object()  # bypass _require_connection; _exec is stubbed
    adapter._exec = lambda query, params=None: rows_by_query.get(query.strip(), [])  # type: ignore[method-assign]
    return adapter


def _rows_for(*, settings: list | None = None) -> dict[str, list]:
    return {
        q.GET_EXTENSIONS.strip(): _EXTENSIONS_ROWS,
        q.GET_DATABASE_PROPERTIES.strip(): _PROPERTIES_ROW,
        q.GET_DATABASE_SETTINGS.strip(): _SETTINGS_ROWS if settings is None else settings,
    }


def test_get_extensions_shape() -> None:
    adapter = _adapter_stubbed(_rows_for())
    extensions = adapter._get_extensions()
    assert extensions == [
        {"name": "citext", "schema": "public", "version": "1.6",
         "comment": "case-insensitive text"},
        {"name": "uuid-ossp", "schema": "extensions", "version": "1.1",
         "comment": None},
    ]


def test_get_database_properties_shape() -> None:
    adapter = _adapter_stubbed(_rows_for())
    assert adapter._get_database_properties() == {
        "encoding": "UTF8",
        "lc_collate": "ru_RU.UTF-8",
        "lc_ctype": "ru_RU.UTF-8",
    }


def test_get_database_properties_empty() -> None:
    adapter = _adapter_stubbed(_rows_for(settings=[]))
    adapter._exec = lambda query, params=None: []  # type: ignore[method-assign]
    assert adapter._get_database_properties() == {}


def test_get_database_settings_splits_param_value() -> None:
    adapter = _adapter_stubbed(_rows_for())
    assert adapter._get_database_settings() == [
        {"name": "work_mem", "value": "64MB"},
        {"name": "statement_timeout", "value": "30s"},
    ]


def test_get_database_settings_empty() -> None:
    adapter = _adapter_stubbed(_rows_for(settings=[]))
    assert adapter._get_database_settings() == []


def test_get_database_settings_skips_malformed() -> None:
    adapter = _adapter_stubbed(_rows_for(settings=[("no_separator_here",), ("=novalue",)]))
    assert adapter._get_database_settings() == []


def test_structure_contains_extensions_and_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """get_database_structure aggregates the new top-level keys (vision §4.2)."""
    adapter = PGDatabaseAdapter()
    adapter._connection = object()
    monkeypatch.setattr(adapter, "_get_schemas", lambda: [])
    monkeypatch.setattr(adapter, "_get_extensions", lambda: _EXTENSIONS_ROWS and [
        {"name": r[0], "schema": r[1], "version": r[2], "comment": r[3]}
        for r in _EXTENSIONS_ROWS
    ])
    monkeypatch.setattr(
        adapter, "_get_database_properties",
        lambda: {"encoding": "UTF8", "lc_collate": "C", "lc_ctype": "C"},
    )
    monkeypatch.setattr(
        adapter, "_get_database_settings",
        lambda: [{"name": "work_mem", "value": "64MB"}],
    )

    structure: dict[str, Any] = adapter.get_database_structure()

    assert structure["extensions"][0]["name"] == "citext"
    assert structure["database"] == {
        "properties": {"encoding": "UTF8", "lc_collate": "C", "lc_ctype": "C"},
        "settings": [{"name": "work_mem", "value": "64MB"}],
    }

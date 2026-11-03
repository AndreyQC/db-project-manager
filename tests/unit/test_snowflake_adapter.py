"""Unit tests for the Snowflake adapter (Phase 21, task 20261103_002).

The driver (snowflake-connector-python) is an optional extra and is NOT
installed in the dev environment — tests inject a fake module into
``sys.modules`` so adapter logic (config/auth mapping, kwargs, stubs,
registry dispatch) is covered without the dependency. Live smoke (real
SSO browser flow) is a user-side check per the plan.
"""

from __future__ import annotations

import sys
from decimal import Decimal
from types import ModuleType
from unittest.mock import MagicMock

import pytest

from db_project_manager.domain.connection import SUPPORTED_DB_TYPES, ConnectionConfig
from db_project_manager.infrastructure.database.base import (
    DatabaseError,
    NotSupportedError,
)
from db_project_manager.infrastructure.database.clickhouse.adapter import CHDatabaseAdapter
from db_project_manager.infrastructure.database.postgres.adapter import PGDatabaseAdapter
from db_project_manager.infrastructure.database.registry import get_adapter
from db_project_manager.infrastructure.database.snowflake.adapter import (
    SnowflakeAdapter,
    build_connect_kwargs,
    extract_account,
)


def _cfg(**overrides: object) -> ConnectionConfig:
    defaults: dict = {
        "host": "xy12345.eu-central-1.snowflakecomputing.com",
        "port": 443,
        "database": "ANALYTICS",
        "username": "user@example.com",
        "password": "",
        "type": "snowflake",
    }
    defaults.update(overrides)
    return ConnectionConfig(**defaults)


class _FakeConnector:
    """Stand-in for ``snowflake.connector`` recording connect kwargs."""

    last_kwargs: dict | None = None
    connect_error: Exception | None = None

    def connect(self, **kwargs: object) -> MagicMock:
        if type(self).connect_error is not None:
            raise type(self).connect_error
        type(self).last_kwargs = kwargs
        return MagicMock(name="snowflake_connection")


@pytest.fixture()
def fake_driver(monkeypatch: pytest.MonkeyPatch) -> _FakeConnector:
    fake = _FakeConnector()
    mod = ModuleType("snowflake")
    conn_mod = ModuleType("snowflake.connector")
    conn_mod.connect = fake.connect
    mod.connector = conn_mod
    monkeypatch.setitem(sys.modules, "snowflake", mod)
    monkeypatch.setitem(sys.modules, "snowflake.connector", conn_mod)
    _FakeConnector.last_kwargs = None
    _FakeConnector.connect_error = None
    return fake


# --- account extraction -------------------------------------------------


def test_account_from_full_host() -> None:
    assert extract_account(_cfg()) == "xy12345.eu-central-1"


def test_account_from_privatelink_host() -> None:
    cfg = _cfg(host="xy12345.eu-central-1.privatelink.snowflakecomputing.com")
    assert extract_account(cfg) == "xy12345.eu-central-1"


def test_account_from_bare_identifier() -> None:
    assert extract_account(_cfg(host="xy12345")) == "xy12345"
    # Region-qualified bare account (no suffix) — returned as-is.
    assert extract_account(_cfg(host="xy12345.eu-central-1")) == "xy12345.eu-central-1"


def test_account_options_win() -> None:
    cfg = _cfg(options={"account": "acme_prod"})
    assert extract_account(cfg) == "acme_prod"


# --- connect kwargs / auth mapping ---------------------------------------


def test_kwargs_password_flow_requires_password() -> None:
    with pytest.raises(DatabaseError, match="требует password"):
        build_connect_kwargs(_cfg())


def test_kwargs_password_flow() -> None:
    kwargs = build_connect_kwargs(_cfg(password="secret"))
    assert kwargs["authenticator"] == "snowflake"
    assert kwargs["password"] == "secret"
    assert kwargs["account"] == "xy12345.eu-central-1"
    assert kwargs["host"] == "xy12345.eu-central-1.snowflakecomputing.com"
    # Default port is not passed; SSO token cache flag belongs to the browser flow.
    assert "port" not in kwargs
    assert "client_store_temporary_credential" not in kwargs


def test_kwargs_browser_flow() -> None:
    kwargs = build_connect_kwargs(_cfg(options={"authenticator": "externalbrowser"}))
    assert kwargs["authenticator"] == "externalbrowser"
    assert kwargs["client_store_temporary_credential"] is True
    assert "password" not in kwargs


def test_kwargs_browser_cache_opt_out() -> None:
    kwargs = build_connect_kwargs(
        _cfg(
            options={
                "authenticator": "externalbrowser",
                "client_store_temporary_credential": False,
            }
        )
    )
    assert "client_store_temporary_credential" not in kwargs


def test_kwargs_warehouse_role_schema_passed() -> None:
    kwargs = build_connect_kwargs(
        _cfg(
            password="pw",
            options={"warehouse": "WH", "role": "RO_LE", "schema": "PUBLIC"},
        )
    )
    assert kwargs["warehouse"] == "WH"
    assert kwargs["role"] == "RO_LE"
    assert kwargs["schema"] == "PUBLIC"


def test_kwargs_custom_port_passed() -> None:
    kwargs = build_connect_kwargs(_cfg(password="pw", port=8443))
    assert kwargs["port"] == 8443


def test_kwargs_bare_account_host_not_passed() -> None:
    kwargs = build_connect_kwargs(_cfg(host="xy12345", password="pw"))
    assert kwargs["account"] == "xy12345"
    assert "host" not in kwargs


def test_kwargs_unknown_authenticator_rejected() -> None:
    with pytest.raises(DatabaseError, match="BACKLOG P3"):
        build_connect_kwargs(_cfg(options={"authenticator": "key-pair"}))


# --- connect / disconnect -----------------------------------------------


def test_connect_browser_flow(fake_driver: _FakeConnector) -> None:
    adapter = SnowflakeAdapter()
    adapter.connect(_cfg(options={"authenticator": "externalbrowser"}))
    assert _FakeConnector.last_kwargs is not None
    assert _FakeConnector.last_kwargs["authenticator"] == "externalbrowser"
    assert adapter._conn is not None
    adapter.disconnect()
    assert adapter._conn is None


def test_connect_sanity_query(fake_driver: _FakeConnector) -> None:
    adapter = SnowflakeAdapter()
    adapter.connect(_cfg(password="pw"))
    conn = adapter._conn
    conn.cursor.return_value.execute.assert_called_once_with("SELECT 1")


def test_connect_driver_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "snowflake", None)
    adapter = SnowflakeAdapter()
    with pytest.raises(DatabaseError, match="uv sync --extra snowflake"):
        adapter.connect(_cfg(options={"authenticator": "externalbrowser"}))


def test_connect_driver_error_wrapped(fake_driver: _FakeConnector) -> None:
    _FakeConnector.connect_error = RuntimeError("SSO timeout")
    adapter = SnowflakeAdapter()
    with pytest.raises(DatabaseError, match="SSO timeout"):
        adapter.connect(_cfg(options={"authenticator": "externalbrowser"}))


# --- run_query (minimal read surface) ------------------------------------


def _connected_adapter_with_cursor(
    fake_driver: _FakeConnector, description: list, rows: list
) -> SnowflakeAdapter:
    adapter = SnowflakeAdapter()
    adapter.connect(_cfg(password="pw"))
    cur = adapter._conn.cursor.return_value
    cur.description = description
    cur.fetchmany.return_value = rows
    return adapter


def test_run_query_basic(fake_driver: _FakeConnector) -> None:
    adapter = _connected_adapter_with_cursor(
        fake_driver, description=[("N",)], rows=[(1,), (2,)]
    )
    result = adapter.run_query("SELECT N FROM t", max_rows=50)
    assert result.columns == ["N"]
    assert result.rows == [{"N": 1}, {"N": 2}]
    assert result.row_count == 2
    assert result.truncated is False


def test_run_query_truncation_and_json_safe(fake_driver: _FakeConnector) -> None:
    adapter = _connected_adapter_with_cursor(
        fake_driver,
        description=[("N",), ("AMT",)],
        rows=[(1, Decimal("10.5")), (2, Decimal("20.5")), (3, Decimal("30.5"))],
    )
    result = adapter.run_query("SELECT N, AMT FROM t", max_rows=2)
    assert result.truncated is True
    assert len(result.rows) == 2
    assert result.rows[0]["AMT"] == "10.5"  # Decimal stringified


def test_run_query_requires_connection() -> None:
    adapter = SnowflakeAdapter()
    with pytest.raises(DatabaseError, match="connect"):
        adapter.run_query("SELECT 1")


# --- MVP stubs -----------------------------------------------------------


def test_re_structure_is_s2_stub() -> None:
    adapter = SnowflakeAdapter()
    with pytest.raises(NotSupportedError, match="S2"):
        adapter.get_database_structure()


def test_deploy_surface_stubs() -> None:
    adapter = SnowflakeAdapter()
    for call in (
        lambda: adapter.check_can_create_db(),
        lambda: adapter.execute_script("SELECT 1"),
        lambda: adapter.create_database("tmp"),
        lambda: adapter.list_schemas(),
    ):
        with pytest.raises(NotSupportedError, match="Phase 21"):
            call()


# --- registry / domain ----------------------------------------------------


def test_registry_dispatches_snowflake() -> None:
    assert isinstance(get_adapter(_cfg()), SnowflakeAdapter)


def test_registry_old_types_unaffected() -> None:
    pg = ConnectionConfig(host="h", port=5432, database="d", username="u", type="postgres")
    gp = ConnectionConfig(host="h", port=5432, database="d", username="u", type="greenplum")
    ch = ConnectionConfig(host="h", port=8123, database="d", username="u", type="clickhouse")
    assert isinstance(get_adapter(pg), PGDatabaseAdapter)
    assert isinstance(get_adapter(gp), PGDatabaseAdapter)
    assert isinstance(get_adapter(ch), CHDatabaseAdapter)


def test_registry_error_lists_snowflake() -> None:
    bad = ConnectionConfig(host="h", port=1, database="d", username="u", type="oracle")
    with pytest.raises(DatabaseError, match="snowflake"):
        get_adapter(bad)


def test_supported_types_contains_snowflake() -> None:
    assert "snowflake" in SUPPORTED_DB_TYPES

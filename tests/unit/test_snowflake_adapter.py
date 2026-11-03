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
from db_project_manager.infrastructure.database.snowflake import queries as sf_q
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


# --- S2: get_database_structure (fixture SHOW/GET_DDL/IS rows) ------------


class _FixtureSnowflake(SnowflakeAdapter):
    """Adapter with SHOW/GET_DDL/INFORMATION_SCHEMA replaced by fixtures."""

    def __init__(self, show: dict, ddl: dict, exec_rows: dict) -> None:
        super().__init__()
        self._conn = object()  # «активное подключение» для _require_connection
        self._cfg = _cfg()
        self._show_map = show
        self._ddl_map = ddl
        self._exec_map = exec_rows

    def connect(self, cfg: ConnectionConfig) -> None:
        """No driver: fixtures are pre-«connected»."""
        self._cfg = cfg

    def disconnect(self) -> None:
        self._conn = None

    def _show(self, command: str, scope: str) -> list[dict]:
        key = (command, scope)
        if key not in self._show_map:
            raise AssertionError(f"неожиданный SHOW: {command!r} {scope!r}")
        return self._show_map[key]

    def _get_ddl(self, kind: str, fq: str) -> str:
        return self._ddl_map.get((kind, fq), "")

    def _exec(self, query: str, params: dict | None = None) -> list[tuple]:
        key = (query.strip(), tuple(sorted((params or {}).items())))
        if key not in self._exec_map:
            raise AssertionError(f"неожиданный запрос: {query.strip()[:40]!r} {params}")
        return self._exec_map[key]


CORE = "ANALYTICS"
SCHEMA = "CORE"
SCHEMA_SCOPE = 'SCHEMA "ANALYTICS"."CORE"'


def _fixture_adapter() -> _FixtureSnowflake:
    show = {
        ("SCHEMAS", 'DATABASE "ANALYTICS"'): [
            {"name": "CORE", "comment": "ядро"},
            {"name": "INFORMATION_SCHEMA", "comment": None},
        ],
        ("TABLES", SCHEMA_SCOPE): [{"name": "ORDERS", "comment": "заказы"}],
        ("VIEWS", SCHEMA_SCOPE): [
            {"name": "V_ORDERS", "comment": "вью заказов", "text": "select id from orders"}
        ],
        ("MATERIALIZED VIEWS", SCHEMA_SCOPE): [
            {"name": "MV_DAILY", "comment": "mv", "text": "select d, count(*) c from orders group by d"}
        ],
        ("SEQUENCES", SCHEMA_SCOPE): [{"name": "SEQ_ORDERS", "comment": None}],
        ("USER FUNCTIONS", SCHEMA_SCOPE): [
            {"name": "FN_ADD", "arguments": "FN_ADD(NUMBER, NUMBER) RETURN NUMBER", "description": "сумма"}
        ],
        ("PROCEDURES", SCHEMA_SCOPE): [
            {"name": "SP_RUN", "arguments": "SP_RUN(NUMBER)", "description": None}
        ],
    }
    ddl = {
        ("SEQUENCE", "ANALYTICS.CORE.SEQ_ORDERS"): (
            "create or replace sequence ANALYTICS.CORE.SEQ_ORDERS start = 10 increment = 5;"
        ),
        ("FUNCTION", "ANALYTICS.CORE.FN_ADD(NUMBER, NUMBER)"): (
            "create or replace FUNCTION ANALYTICS.CORE.FN_ADD(a number, b number) "
            "returns number language sql as 'select a + b';"
        ),
        ("PROCEDURE", "ANALYTICS.CORE.SP_RUN(NUMBER)"): (
            "create or replace PROCEDURE ANALYTICS.CORE.SP_RUN(x number) "
            "returns string language sql as 'begin return to_string(x); end';"
        ),
    }
    exec_rows = {
        (sf_q.GET_COLUMNS.strip(), (("schema", "CORE"), ("table", "ORDERS"))): [
            ("ID", "NUMBER", "NO", None, None, 38, 0, "идентификатор"),
            ("NAME", "VARCHAR", "YES", None, 100, None, None, None),
            ("CUST_ID", "NUMBER", "YES", "CORE.SQ_CUST.NEXTVAL", None, 38, 0, None),
        ],
        (sf_q.GET_COLUMNS.strip(), (("schema", "CORE"), ("table", "V_ORDERS"))): [
            ("ID", "NUMBER", "YES", None, None, 38, 0, None),
        ],
        (sf_q.GET_COLUMNS.strip(), (("schema", "CORE"), ("table", "MV_DAILY"))): [],
        (sf_q.GET_CONSTRAINTS.strip(), (("schema", "CORE"), ("table", "ORDERS"))): [
            ("PK_ORDERS", "PRIMARY KEY", "ORDERS", "ID", None, None, None, None),
            ("FK_ORDERS", "FOREIGN KEY", "ORDERS", "CUST_ID", "PK_CUST", "CORE", "CUSTOMERS", "ID"),
        ],
    }
    return _FixtureSnowflake(show, ddl, exec_rows)


def test_structure_shape_and_information_schema_excluded() -> None:
    structure = _fixture_adapter().get_database_structure()
    names = [s["name"] for s in structure["schemas"]]
    assert names == ["CORE"]
    core = structure["schemas"][0]
    assert core["comment"] == "ядро"
    assert core["indexes"] == [] and core["enums"] == []
    assert [t["name"] for t in core["tables"]] == ["ORDERS"]
    assert [v["name"] for v in core["views"]] == ["V_ORDERS"]
    assert [m["name"] for m in core["materialized_views"]] == ["MV_DAILY"]
    assert [s["name"] for s in core["sequences"]] == ["SEQ_ORDERS"]
    assert [f["name"] for f in core["functions"]] == ["FN_ADD"]
    assert [p["name"] for p in core["procedures"]] == ["SP_RUN"]


def test_table_columns_types_and_autoincrement_dropped() -> None:
    table = _fixture_adapter().get_database_structure()["schemas"][0]["tables"][0]
    assert table["comment"] == "заказы"
    cols = {c["name"]: c for c in table["columns"]}
    assert cols["ID"]["type"] == "NUMBER" and cols["ID"]["nullable"] is False
    assert cols["NAME"]["character_maximum_length"] == 100
    # Snowflake autoincrement (<seq>.NEXTVAL default) is dropped in MVP.
    assert cols["CUST_ID"]["default"] is None
    assert cols["ID"]["comment"] == "идентификатор"


def test_constraint_definitions_rendered() -> None:
    constraints = {
        c["name"]: c
        for c in _fixture_adapter().get_database_structure()["schemas"][0]["tables"][0][
            "constraints"
        ]
    }
    assert constraints["PK_ORDERS"]["definition"] == "PRIMARY KEY (ID)"
    assert (
        constraints["FK_ORDERS"]["definition"]
        == "FOREIGN KEY (CUST_ID) REFERENCES CORE.CUSTOMERS (ID)"
    )


def test_view_matview_definition_from_show_text() -> None:
    schema = _fixture_adapter().get_database_structure()["schemas"][0]
    view = schema["views"][0]
    # Leading space added by the adapter: templates render "AS{{ definition }}".
    assert view["definition"].strip() == "select id from orders"
    assert view["columns"][0]["name"] == "ID"
    mv = schema["materialized_views"][0]
    assert mv["definition"].strip() == "select d, count(*) c from orders group by d"
    assert mv["tablespace"] is None  # PG-only clause skipped


def test_sequence_fields_from_get_ddl() -> None:
    seq = _fixture_adapter().get_database_structure()["schemas"][0]["sequences"][0]
    assert seq["start"] == 10
    assert seq["increment"] == 5
    assert seq["minvalue"] is None and seq["maxvalue"] is None


def test_routines_signature_and_ddl_body() -> None:
    schema = _fixture_adapter().get_database_structure()["schemas"][0]
    fn = schema["functions"][0]
    assert fn["argument_types"] == "NUMBER, NUMBER"
    assert "FUNCTION ANALYTICS.CORE.FN_ADD" in fn["definition"]
    proc = schema["procedures"][0]
    assert proc["argument_types"] == "NUMBER"
    assert "PROCEDURE ANALYTICS.CORE.SP_RUN" in proc["definition"]


def test_invalid_schema_name_rejected() -> None:
    show = {("SCHEMAS", 'DATABASE "ANALYTICS"'): [{"name": 'bad"; drop', "comment": None}]}
    adapter = _FixtureSnowflake(show, {}, {})
    with pytest.raises(DatabaseError, match="Недопустимое имя"):
        adapter.get_database_structure()


# --- S2: RE end-to-end on fixtures ----------------------------------------


def test_reverse_engineer_renders_snowflake_files(tmp_path) -> None:
    from db_project_manager.application.reverse_engineer import ReverseEngineerService

    adapter = _fixture_adapter()
    service = ReverseEngineerService(adapter_factory=lambda _cfg: adapter)
    target = service.run(_cfg(), tmp_path)

    assert (target / "dbpm.manifest.json").is_file()
    # Schema file + table with inline PK and FK via ALTER.
    schema_sql = (target / "CORE" / "schema CORE.sql").read_text(encoding="utf-8")
    assert 'CREATE SCHEMA "CORE"' in schema_sql
    table_sql = (target / "CORE" / "tables" / "table ORDERS.sql").read_text(encoding="utf-8")
    assert 'CREATE TABLE "CORE"."ORDERS"' in table_sql
    assert 'CONSTRAINT "PK_ORDERS" PRIMARY KEY (ID)' in table_sql
    assert (
        'ADD CONSTRAINT "FK_ORDERS" FOREIGN KEY (CUST_ID) REFERENCES CORE.CUSTOMERS (ID)'
        in table_sql
    )
    # View takes its query from SHOW text.
    view_sql = (target / "CORE" / "views" / "view V_ORDERS.sql").read_text(encoding="utf-8")
    assert "CREATE OR REPLACE VIEW" in view_sql
    assert "select id from orders" in view_sql
    # Matview without the PG-only TABLESPACE clause.
    mv_sql = (target / "CORE" / "materialized_views" / "materialized_view MV_DAILY.sql").read_text(
        encoding="utf-8"
    )
    assert "CREATE MATERIALIZED VIEW" in mv_sql
    # The PG-only clause is skipped: view header goes straight to AS <query>.
    assert 'CREATE MATERIALIZED VIEW "CORE"."MV_DAILY"\nAS select d' in mv_sql
    # Sequences without PG-only MINVALUE/MAXVALUE/CACHE clauses.
    seq_sql = (target / "CORE" / "sequences" / "sequence SEQ_ORDERS.sql").read_text(encoding="utf-8")
    assert "START 10" in seq_sql and "INCREMENT BY 5" in seq_sql
    # Rendered clauses would carry values ("MINVALUE 1", "CACHE 1") — template
    # comments mention the clause names, the values do not appear.
    assert "MINVALUE 1" not in seq_sql and "CACHE 1" not in seq_sql
    # Routine bodies carry the GET_DDL definition.
    fn_sql = (target / "CORE" / "functions" / "function FN_ADD.sql").read_text(encoding="utf-8")
    assert "FUNCTION ANALYTICS.CORE.FN_ADD" in fn_sql
    # __deploy service schema seeded into the codebase (PG canonical templates).
    assert (target / "__deploy" / "schema __deploy.sql").is_file()

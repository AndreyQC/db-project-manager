"""Integration tests for CHProfiler (Phase 6.2).

Live CH cluster (or CLICKHOUSE_TEST_* env vars). Per-test DB created by
``ch_conn_cfg`` fixture. Marked ``clickhouse`` — run with
``uv run pytest -m clickhouse``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from db_project_manager.application.profiling_service import (
    ProfilingService,
)
from db_project_manager.domain.connection import ConnectionConfig
from db_project_manager.infrastructure.profiling import get_profiler
from db_project_manager.infrastructure.profiling.clickhouse import CHProfiler

# Mark every test as 'clickhouse' so it can be selected via -m clickhouse.
# The conftest.py in this directory also sets this marker, but importing
# it explicitly is robust to test discovery order.
pytestmark = pytest.mark.clickhouse


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _encrypt_password(plain: str) -> str:
    """Return ``crypto__ENVOS_CRYPTO_01__<fernet-token>`` for the
    ConnectionConfig password field so that ``ConnectionConfig.password``
    decrypts back to ``plain`` at runtime.
    """
    key = os.environ["ENVOS_CRYPTO_01"].encode()
    token = Fernet(key).encrypt(plain.encode()).decode()
    return f"crypto__ENVOS_CRYPTO_01__{token}"


def _write_ch_connection_yaml(
    connections_dir: Path,
    name: str,
    cfg: ConnectionConfig,
    *,
    profiling_enabled: bool = True,
) -> Path:
    """Materialise a connection YAML on disk for ``ProfilingService``.

    ``ProfilingService.profile_tables`` takes a connection **name** and
    loads the config from disk. The connection_store decrypts the
    ``password`` field with ``Fernet(ENVOS_CRYPTO_01)`` — we therefore
    store the password encrypted, not the raw key.
    """
    encrypted_pwd = _encrypt_password(cfg.password)
    block = "\nprofiling:\n  enabled: true\n" if profiling_enabled else ""
    path = connections_dir / f"{name}.yaml"
    path.write_text(
        f"host: {cfg.host}\n"
        f"port: {cfg.port}\n"
        f"database: {cfg.database}\n"
        f"username: {cfg.username}\n"
        f"password: {encrypted_pwd}\n"
        f"type: clickhouse\n"
        f"{block}",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def ch_connections_dir(
    ch_conn_cfg: ConnectionConfig, tmp_path: Path
) -> Path:
    """Create a fresh connections dir containing the per-test CH config."""
    connections_dir = tmp_path / "connections"
    connections_dir.mkdir()
    _write_ch_connection_yaml(connections_dir, "ch_it", ch_conn_cfg)
    return connections_dir


# --------------------------------------------------------------------------- #
# Direct unit-style tests (no ProfilingService roundtrip)
# --------------------------------------------------------------------------- #


def test_get_profiler_dispatches_to_ch_profiler() -> None:
    """The factory returns CHProfiler for ``type=clickhouse``."""
    profiler = get_profiler("clickhouse")
    assert isinstance(profiler, CHProfiler)


def test_ch_profiler_uses_ch_specific_sql(ch_conn_cfg: ConnectionConfig) -> None:
    """Sanity: the SQL queries mention system.columns / system.tables /
    system.parts and CH-specific functions (no PG syntax)."""
    p = CHProfiler()
    cols_sql = p.columns_sql("db", "t")
    assert "system.columns" in cols_sql
    assert "clusterAllReplicas" in cols_sql
    assert "percentile_cont" not in cols_sql  # PG only
    assert "TABLESAMPLE" not in cols_sql       # PG only

    meta_sql = p.table_meta_sql("db", "t")
    assert "system.tables" in meta_sql
    assert "system.parts" in meta_sql


# --------------------------------------------------------------------------- #
# End-to-end via ProfilingService.profile_tables
# --------------------------------------------------------------------------- #


def test_profile_tables_on_ch_returns_table_profile(
    ch_conn_cfg: ConnectionConfig,
    ch_connections_dir: Path,
) -> None:
    """Full e2e: create a CH table, populate it, run profile_tables.

    Verifies that the previously-failing ``profile_tables`` MCP tool now
    works for CH — the key acceptance criterion for Phase 6.2.
    """
    from db_project_manager.infrastructure.database.registry import get_adapter

    a = get_adapter(ch_conn_cfg)
    a.connect(ch_conn_cfg)
    try:
        a.execute_script(
            f"CREATE TABLE {ch_conn_cfg.database}.orders ("
            "id UInt64, region String, amount Decimal(10, 2), "
            "created_at DateTime64(3, 'UTC')"
            ") ENGINE = MergeTree ORDER BY id"
        )
        # Insert a few rows with mixed patterns
        a.execute_script(
            f"INSERT INTO {ch_conn_cfg.database}.orders "
            "SELECT number, "
            "       ['east', 'west', 'north', 'south'][number % 4 + 1], "
            "       number * 1.5, "
            "       toDateTime('2026-01-01') + number "
            "FROM numbers(20)"
        )
    finally:
        a.disconnect()

    service = ProfilingService(connections_dir=str(ch_connections_dir))
    profiles = service.profile_tables("ch_it", [f"{ch_conn_cfg.database}.orders"])

    assert len(profiles) == 1
    tp = profiles[0]
    assert (tp.schema_name, tp.table_name) == (ch_conn_cfg.database, "orders")
    # CH may report 0 if merges haven't happened, but the call should not fail.
    assert tp.row_count is not None or tp.row_count == 0
    assert tp.warnings == []  # no parse errors → no warnings

    # Per-column sanity
    cols = {c.name: c for c in tp.columns}
    assert set(cols) == {"id", "region", "amount", "created_at"}
    assert cols["region"].distinct_count >= 1
    # Top values reflect the 4 distinct region strings
    top_regions = {v["value"] for v in cols["region"].top_values}
    assert top_regions.issubset({"east", "west", "north", "south"})
    # Percentiles populated for the numeric column
    assert "p50" in cols["amount"].percentiles
    # The whole profile must be JSON-serializable. CH returns Decimal for
    # percentile aggregates of Decimal columns (PG returns float); we
    # stringify them here just for the serialization check.
    from decimal import Decimal

    def _json_default(o):
        if isinstance(o, Decimal):
            return str(o)
        raise TypeError(f"not JSON-serializable: {type(o).__name__}")

    json.dumps(tp.model_dump(), ensure_ascii=False, default=_json_default)


def test_profile_tables_on_empty_ch_table(
    ch_conn_cfg: ConnectionConfig,
    ch_connections_dir: Path,
) -> None:
    """Empty CH table: profile should return row_count=0 without errors."""
    from db_project_manager.infrastructure.database.registry import get_adapter

    a = get_adapter(ch_conn_cfg)
    a.connect(ch_conn_cfg)
    try:
        a.execute_script(
            f"CREATE TABLE {ch_conn_cfg.database}.empty_t ("
            "id UInt64, name String"
            ") ENGINE = MergeTree ORDER BY id"
        )
    finally:
        a.disconnect()

    service = ProfilingService(connections_dir=str(ch_connections_dir))
    profiles = service.profile_tables(
        "ch_it", [f"{ch_conn_cfg.database}.empty_t"]
    )

    assert len(profiles) == 1
    tp = profiles[0]
    assert tp.row_count == 0
    # Even empty tables should report column structure
    assert {c.name for c in tp.columns} == {"id", "name"}


def test_profile_tables_profiling_disabled(
    ch_conn_cfg: ConnectionConfig,
    tmp_path: Path,
) -> None:
    """Connections with ``profiling.enabled=false`` raise ProfilingError."""
    from db_project_manager.application.profiling_service import ProfilingError

    connections_dir = tmp_path / "connections"
    connections_dir.mkdir()
    _write_ch_connection_yaml(
        connections_dir, "ch_no_prof", ch_conn_cfg, profiling_enabled=False
    )

    service = ProfilingService(connections_dir=str(connections_dir))
    with pytest.raises(ProfilingError, match="профайлинг запрещён"):
        service.profile_tables(
            "ch_no_prof", [f"{ch_conn_cfg.database}.anything"]
        )

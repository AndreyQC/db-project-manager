"""Integration-test fixtures for ClickHouse: live CH cluster + per-test DB.

Slow: requires either a live ClickHouse server reachable via
``clickhouse-connect`` or a testcontainers bootstrap. Run with
``uv run pytest -m clickhouse``.

The fixture design mirrors ``conftest.py`` (PG) but uses a per-test
*database* (not a per-test schema) because CH has no schemas in the
PG sense — ``CREATE DATABASE`` is the only isolation primitive.

**Connection priority:**

1. If ``CLICKHOUSE_TEST_HOST`` / ``_PORT`` / ``_USER`` / ``_PASSWORD`` are
   set, the fixture connects to that endpoint (the project's staging
   cluster, a dev VM, etc.). The user must have ``CREATE DATABASE`` /
   ``DROP DATABASE`` rights.
2. Otherwise, the fixture expects the project's own
   ``connections/10_41_254_51_CH__DB__default__U_default.yaml`` (the
   ``default`` superuser connection) and uses it directly. This is the
   path CI / dev machines will take until a docker-compose is added.
3. testcontainers / Docker is NOT used: the existing PG container is
   not portable to a CI runner that may have a different Docker image
   surface, and we already have a working shared CH. Adding a
   testcontainers CH is future work (Phase 5).
"""

from __future__ import annotations

import os
import uuid
import warnings
from pathlib import Path
from typing import Any

import pytest

# Mark every test that uses these fixtures as 'clickhouse' so it can be
# selected or deselected (see pyproject.toml markers).
pytestmark = pytest.mark.clickhouse

#: Path to the default-superuser connection shipped with the project.
DEFAULT_CONN_PATH = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "connections"
    / "10_41_254_51_CH__DB__default__U_default.yaml"
)


def _load_default_conn() -> dict[str, Any]:
    """Decrypt and return the default-superuser connection dict.

    Honours ``CLICKHOUSE_TEST_*`` env vars (override per-test endpoint);
    falls back to the project's ``default`` connection.
    """
    host = os.environ.get("CLICKHOUSE_TEST_HOST")
    if host:
        return {
            "host": host,
            "port": int(os.environ.get("CLICKHOUSE_TEST_PORT", "8123")),
            "username": os.environ["CLICKHOUSE_TEST_USER"],
            "password": os.environ["CLICKHOUSE_TEST_PASSWORD"],
            "database": os.environ.get("CLICKHOUSE_TEST_DATABASE", "default"),
            "secure": bool(int(os.environ.get("CLICKHOUSE_TEST_SECURE", "0"))),
        }

    # Fall back to the project-shipped connection.
    import yaml
    from cryptography.fernet import Fernet

    if not DEFAULT_CONN_PATH.exists():
        pytest.skip(
            f"ClickHouse test connection not configured. Set "
            f"CLICKHOUSE_TEST_HOST/PORT/USER/PASSWORD, or place a "
            f"connection at {DEFAULT_CONN_PATH}."
        )

    with open(DEFAULT_CONN_PATH) as f:
        raw = yaml.safe_load(f)
    key = os.environ.get("ENVOS_CRYPTO_01")
    if not key:
        pytest.skip("ENVOS_CRYPTO_01 not set; cannot decrypt CH password.")
    token = raw["password"].replace("crypto__ENVOS_CRYPTO_01__", "").encode()
    raw["password"] = Fernet(key.encode()).decrypt(token).decode()
    return raw


@pytest.fixture
def ch_conn_cfg() -> Any:
    """A ConnectionConfig pointing at a FRESH per-test database.

    Each test gets its own database, created and dropped here — the
    cluster's per-shard desync (see checkpoint 20261017_001) makes
    sharing state across tests flaky, and DDL is the easiest way to
    reset. Mirrors the per-test-DB pattern from ``pg_conn_cfg``.
    """
    from db_project_manager.domain.connection import ConnectionConfig
    from db_project_manager.infrastructure.database.registry import get_adapter

    raw = _load_default_conn()
    admin_cfg = ConnectionConfig(
        host=raw["host"],
        port=raw["port"],
        database=raw.get("database", "default"),
        username=raw["username"],
        password=raw["password"],
        type="clickhouse",
    )
    db_name = f"ch_it_{uuid.uuid4().hex[:12]}"
    admin = get_adapter(admin_cfg)
    admin.connect(admin_cfg)
    admin.create_database(db_name)
    admin.disconnect()
    try:
        yield admin_cfg.model_copy(update={"database": db_name})
    finally:
        try:
            admin.connect(admin_cfg)
            admin.drop_database(db_name)
        except Exception as e:  # noqa: BLE001 — teardown must not mask the test result
            warnings.warn(f"Не удалось удалить тестовую CH БД {db_name}: {e}")
        finally:
            admin.disconnect()


@pytest.fixture
def ch_admin_conn_cfg() -> Any:
    """The admin ConnectionConfig (no specific DB) — for cluster-level ops
    (creating / dropping test DBs, reading system.tables, etc.)."""
    from db_project_manager.domain.connection import ConnectionConfig

    raw = _load_default_conn()
    return ConnectionConfig(
        host=raw["host"],
        port=raw["port"],
        database=raw.get("database", "default"),
        username=raw["username"],
        password=raw["password"],
        type="clickhouse",
    )

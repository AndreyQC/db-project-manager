"""Integration-test fixtures: a real PostgreSQL in a Docker container.

Slow: requires Docker. Run with 'uv run pytest -m integration'.
Skipped by default in 'uv run pytest' (markers configured in pyproject).
"""

from __future__ import annotations

from pathlib import Path

import pytest

# Mark every test in this directory as 'integration' so it can be selected
# or deselected (see pyproject.toml [tool.pytest.ini_options] markers).
pytestmark = pytest.mark.integration


@pytest.fixture(scope="session")
def pg_container():
    """Start a fresh PostgreSQL 16 container for the test session."""
    pytest.importorskip("testcontainers")
    from testcontainers.postgres import PostgresContainer

    # The postgres image ships a superuser 'postgres'; we set a known password
    # so the user has CREATEDB (needed by DeployValidateService).
    container = PostgresContainer("postgres:16", username="postgres", password="postgres", dbname="postgres")
    container.start()
    try:
        yield container
    finally:
        container.stop()


@pytest.fixture
def pg_conn_cfg(pg_container):
    """A ConnectionConfig pointing at the live container (CREATEDB user)."""
    from db_project_manager.domain.connection import ConnectionConfig

    return ConnectionConfig(
        host=pg_container.get_container_host_ip(),
        port=int(pg_container.get_exposed_port(pg_container.port)),
        database="postgres",
        username="postgres",
        password="postgres",
        type="postgres",
    )


@pytest.fixture
def codebase_sample_dir() -> Path:
    """The shared codebase fixture (bookings schema with autodoc headers)."""
    return Path(__file__).resolve().parent.parent / "fixtures" / "codebase_sample"

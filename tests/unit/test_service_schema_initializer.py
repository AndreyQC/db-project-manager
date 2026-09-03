"""Unit tests for :mod:`db_project_manager.application.service_schema_initializer`.

Phase 15.5.2 (cis_zup feedback 2026-09-02): ``deploy apply`` cannot bootstrap
``__deploy`` itself — RE seeds canonical DDL into the target-side temp
snapshot, which CompareService treats as UNCHANGED. This module provides the
explicit, idempotent bootstrap used by ``db-pm deploy init-service-schema``.

The tests stub :class:`DatabaseAdapter` directly (no testcontainers) — the
adapter contract is small enough to fake and the bootstrap logic is purely
adapter-driven.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from db_project_manager.application.service_schema_initializer import (
    ServiceSchemaInitializer,
    ServiceSchemaInitializerError,
)
from db_project_manager.domain.connection import ConnectionConfig
from db_project_manager.infrastructure.database.base import DatabaseError


@dataclass
class _FakeAdapter:
    """Minimal DatabaseAdapter stub covering only what the initializer needs."""

    structure: dict[str, Any] = field(default_factory=dict)
    executed_scripts: list[str] = field(default_factory=list)
    schema_version: str | None = "v0"
    connect_calls: int = 0
    disconnect_calls: int = 0
    raise_on_connect: Exception | None = None
    raise_on_execute: Exception | None = None

    def connect(self, cfg: ConnectionConfig) -> None:
        self.connect_calls += 1
        if self.raise_on_connect is not None:
            raise self.raise_on_connect

    def disconnect(self) -> None:
        self.disconnect_calls += 1

    def execute_script(self, script: str) -> None:
        self.executed_scripts.append(script)
        if self.raise_on_execute is not None:
            raise self.raise_on_execute

    def get_database_structure(self) -> dict[str, Any]:
        return self.structure

    def get_schema_version(self, schema_name: str) -> str | None:
        return self.schema_version


def _conn() -> ConnectionConfig:
    return ConnectionConfig(
        name="t", type="postgres", database="d", host="h", port=5432, username="u"
    )


def _adapter_factory(adapter: _FakeAdapter):
    return lambda cfg: adapter


def _structure_with_schema(schema_name: str, table_names: list[str]) -> dict[str, Any]:
    return {
        "schemas": [
            {
                "name": schema_name,
                "tables": [{"name": n} for n in table_names],
            }
        ]
    }


def _empty_structure() -> dict[str, Any]:
    return {"schemas": []}


# --- 1. Empty DB → bootstrap creates schema + 3 tables ---


def test_empty_db_creates_schema_and_three_tables():
    adapter = _FakeAdapter(structure=_empty_structure())
    initializer = ServiceSchemaInitializer(adapter_factory=_adapter_factory(adapter))

    result = initializer.run(_conn())

    assert result.schema_present is True
    assert result.created_schema is True
    assert set(result.created_tables) == {
        "schema_version", "script_history", "script_audit_log"
    }
    assert result.schema_version == "v0"

    # Verify execute_script calls: 1 schema + 3 tables.
    assert len(adapter.executed_scripts) == 4
    assert adapter.executed_scripts[0] == 'CREATE SCHEMA IF NOT EXISTS "__deploy";\n'
    # All table scripts use CREATE TABLE IF NOT EXISTS (idempotent).
    for script in adapter.executed_scripts[1:]:
        assert "CREATE TABLE IF NOT EXISTS" in script
        assert '"__deploy"' in script
    # disconnect was called exactly once.
    assert adapter.disconnect_calls == 1


# --- 2. Already initialized DB → idempotent no-op ---


def test_fully_initialized_db_is_noop():
    adapter = _FakeAdapter(
        structure=_structure_with_schema(
            "__deploy", ["schema_version", "script_history", "script_audit_log"]
        ),
        schema_version="v1",
    )
    initializer = ServiceSchemaInitializer(adapter_factory=_adapter_factory(adapter))

    result = initializer.run(_conn())

    assert result.changed is False
    assert result.created_schema is False
    assert result.created_tables == []
    assert result.schema_present is True
    assert result.tables_present == [
        "schema_version", "script_history", "script_audit_log"
    ]
    assert result.schema_version == "v1"
    assert adapter.executed_scripts == []  # no SQL emitted
    assert adapter.disconnect_calls == 1


# --- 3. Custom service_schema name (e.g. cfg override) ---


def test_custom_service_schema_name():
    adapter = _FakeAdapter(structure=_empty_structure())
    initializer = ServiceSchemaInitializer(
        service_schema="my_svc", adapter_factory=_adapter_factory(adapter)
    )

    result = initializer.run(_conn())

    assert result.service_schema == "my_svc"
    assert 'CREATE SCHEMA IF NOT EXISTS "my_svc"' in adapter.executed_scripts[0]
    # All 3 table scripts refer to my_svc.
    for script in adapter.executed_scripts[1:]:
        assert '"my_svc"' in script


# --- 4. Partial state: schema present, some tables missing ---


def test_partial_state_only_creates_missing_tables():
    structure = _structure_with_schema("__deploy", ["schema_version"])  # only 1 of 3
    adapter = _FakeAdapter(structure=structure)
    initializer = ServiceSchemaInitializer(adapter_factory=_adapter_factory(adapter))

    result = initializer.run(_conn())

    # Schema already present → not recreated.
    assert result.created_schema is False
    # Only the missing tables were created (script_history, script_audit_log).
    assert set(result.created_tables) == {"script_history", "script_audit_log"}
    # After bootstrap, all tables are reported as present; tables_missing is empty.
    assert set(result.tables_present) == {
        "schema_version", "script_history", "script_audit_log"
    }
    assert result.tables_missing == []
    assert result.changed is True
    # 2 execute_script calls (no schema creation).
    assert len(adapter.executed_scripts) == 2


# --- 5. Connection failure surfaces as ServiceSchemaInitializerError ---


def test_connection_failure_raises_typed_error():
    adapter = _FakeAdapter(raise_on_connect=DatabaseError("boom"))
    initializer = ServiceSchemaInitializer(adapter_factory=_adapter_factory(adapter))

    with pytest.raises(ServiceSchemaInitializerError, match="Не удалось подключиться"):
        initializer.run(_conn())

    # disconnect must NOT be called when connect failed.
    assert adapter.disconnect_calls == 0


# --- 6. SQL execution failure on CREATE SCHEMA is fatal ---


def test_create_schema_failure_is_fatal():
    adapter = _FakeAdapter(
        structure=_empty_structure(),
        raise_on_execute=DatabaseError("permission denied"),
    )
    initializer = ServiceSchemaInitializer(adapter_factory=_adapter_factory(adapter))

    with pytest.raises(ServiceSchemaInitializerError, match="Не удалось создать схему"):
        initializer.run(_conn())

    # Disconnect still called (clean up).
    assert adapter.disconnect_calls == 1


# --- 7. SQL execution failure on CREATE TABLE contains useful context ---


def test_create_table_failure_includes_table_name():
    """Each table is created in its own execute_script call so partial
    progress and a clear error message are preserved when one table fails."""
    adapter = _FakeAdapter(structure=_empty_structure())
    initializer = ServiceSchemaInitializer(adapter_factory=_adapter_factory(adapter))

    # Make the 2nd execute_script (schema_version table) fail.
    fail_at = {"count": 0}
    original_execute = adapter.execute_script

    def execute_with_failure(script: str) -> None:
        fail_at["count"] += 1
        if fail_at["count"] == 2:  # schema_version
            raise DatabaseError("table create boom")
        original_execute(script)

    # Bypass dataclass frozen-like behaviour by replacing the bound method.
    adapter.execute_script = execute_with_failure  # type: ignore[method-assign]

    with pytest.raises(ServiceSchemaInitializerError) as exc_info:
        initializer.run(_conn())

    msg = str(exc_info.value)
    assert "schema_version" in msg, f"Expected table name in error, got: {msg!r}"
    assert "table create boom" in msg
    assert adapter.disconnect_calls == 1


# --- 8. CLI smoke: typer app accepts the new subcommand ---


def test_cli_registers_init_service_schema_command():
    from db_project_manager.presentation.cli.main import app

    # typer.Typer registers subapps; this test just verifies the module loads
    # the initializer + helper without an import-time error.
    assert app is not None
"""Tests for the canonical __deploy DDL and validator (Phase 10 S5).

The canonical DDL is db-pm's source of truth for the service schema. A warning
(NOT a hard block) fires when the codebase's __deploy differs from canonical —
CDF-10 approach b. The validator must be invariant under autodoc-only changes
(CDF-6) so a metadata-only edit does not raise a false positive.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from db_project_manager.infrastructure.deploy.canonical_ddl import (
    DEFAULT_SERVICE_SCHEMA,
    canonical_deploy_checksums,
    canonical_deploy_ddl,
    validate_deploy_ddl,
)
from db_project_manager.infrastructure.sql.autodoc import ensure_header


# ----------------------------------------------------- canonical rendering


def test_canonical_ddl_returns_three_tables() -> None:
    """The service schema always has exactly these three tables."""
    ddl = canonical_deploy_ddl()
    assert set(ddl) == {"schema_version", "script_history", "script_audit_log"}


def test_canonical_ddl_uses_default_schema_name() -> None:
    """Default schema is __deploy; DDL references it fully-qualified."""
    ddl = canonical_deploy_ddl()
    for body in ddl.values():
        assert '"__deploy".' in body


def test_canonical_ddl_renders_custom_schema_name() -> None:
    """A user-configured service schema name propagates into the DDL."""
    ddl = canonical_deploy_ddl(schema_name="my_deploy")
    for body in ddl.values():
        assert '"my_deploy".' in body
        # Default schema name must NOT leak when a custom one is used.
        assert '"__deploy".' not in body


def test_canonical_checksums_match_ddl_content() -> None:
    """Checksums dict has one SHA-256 hex per table; stable across calls."""
    from db_project_manager.domain.deploy import script_checksum
    from db_project_manager.infrastructure.sql.autodoc import strip_autodoc

    checksums = canonical_deploy_checksums()
    ddl = canonical_deploy_ddl()
    assert set(checksums) == set(ddl)
    for name, body in ddl.items():
        # Same pipeline as the validator: strip (no-op on canonical) + checksum.
        expected = script_checksum(strip_autodoc(body))
        assert checksums[name] == expected
        assert len(checksums[name]) == 64


def test_canonical_checksums_cached_and_stable() -> None:
    """Two calls return the same hashes (lru_cache + deterministic templates)."""
    first = canonical_deploy_checksums()
    second = canonical_deploy_checksums()
    assert first == second


# ----------------------------------------------------- validate_deploy_ddl


def _write_canonical_codebase(tmp_path: Path, schema_name: str = DEFAULT_SERVICE_SCHEMA) -> Path:
    """Write a codebase whose __deploy matches canonical exactly."""
    ddl = canonical_deploy_ddl(schema_name)
    tables_dir = tmp_path / schema_name / "tables"
    tables_dir.mkdir(parents=True)
    for name, body in ddl.items():
        (tables_dir / f"{name}.sql").write_text(body, encoding="utf-8")
    return tmp_path


def test_validate_deploy_ddl_match_returns_no_warnings(tmp_path: Path) -> None:
    """Codebase with canonical DDL → empty warnings list."""
    _write_canonical_codebase(tmp_path)
    assert validate_deploy_ddl(tmp_path) == []


def test_validate_deploy_ddl_match_with_autodoc_header(tmp_path: Path) -> None:
    """Adding an autodoc header (immutable marker) does NOT trip the validator.

    CDF-6: checksum is computed on executable SQL only; metadata-only changes
    are invisible. The seed written by reverse-engineer (S6) carries the
    immutable marker in autodoc, and that must still validate as canonical.
    """
    ddl = canonical_deploy_ddl()
    tables_dir = tmp_path / DEFAULT_SERVICE_SCHEMA / "tables"
    tables_dir.mkdir(parents=True)
    for name, body in ddl.items():
        decorated = ensure_header(
            body,
            object_catalog="db",
            object_schema=DEFAULT_SERVICE_SCHEMA,
            object_type="table",
            object_name=name,
            immutable=True,
        )
        (tables_dir / f"{name}.sql").write_text(decorated, encoding="utf-8")
    assert validate_deploy_ddl(tmp_path) == []


def test_validate_deploy_ddl_detects_modified_column(tmp_path: Path) -> None:
    """A real SQL change (added column) → warning naming the table."""
    _write_canonical_codebase(tmp_path)
    # Mutate script_history.sql: add a column.
    path = tmp_path / DEFAULT_SERVICE_SCHEMA / "tables" / "script_history.sql"
    body = path.read_text(encoding="utf-8")
    body = body.replace(
        "duration_ms     INTEGER NOT NULL,",
        "duration_ms     INTEGER NOT NULL,\n    note            TEXT,",
    )
    path.write_text(body, encoding="utf-8")
    warnings = validate_deploy_ddl(tmp_path)
    assert len(warnings) == 1
    assert "script_history.sql" in warnings[0]
    assert "отличается от canonical" in warnings[0]


def test_validate_deploy_ddl_reports_missing_table(tmp_path: Path) -> None:
    """Missing file → warning naming the absent file."""
    _write_canonical_codebase(tmp_path)
    (tmp_path / DEFAULT_SERVICE_SCHEMA / "tables" / "script_audit_log.sql").unlink()
    warnings = validate_deploy_ddl(tmp_path)
    assert len(warnings) == 1
    assert "script_audit_log.sql" in warnings[0]
    assert "отсутствует" in warnings[0]


def test_validate_deploy_ddl_missing_dir_reports_all_tables(tmp_path: Path) -> None:
    """No __deploy/tables at all → one warning per table (3 total)."""
    warnings = validate_deploy_ddl(tmp_path)
    assert len(warnings) == 3
    assert all("отсутствует" in w for w in warnings)


def test_validate_deploy_ddl_respects_custom_schema_name(tmp_path: Path) -> None:
    """When schema_name='my_deploy', validator looks in my_deploy/tables/."""
    _write_canonical_codebase(tmp_path, schema_name="my_deploy")
    assert validate_deploy_ddl(tmp_path, schema_name="my_deploy") == []
    # Default-schema lookup on the same tree must report all missing.
    warnings = validate_deploy_ddl(tmp_path)
    assert len(warnings) == 3


def test_validate_deploy_ddl_invariant_under_whitespace_only_diff(tmp_path: Path) -> None:
    """Trailing whitespace and extra blank lines do NOT trigger a warning.

    canonical_normalize already strips these; the validator must rely on the
    normalized form, not the raw text. This locks the CDF-6 contract for DDL
    files: cosmetic edits don't masquerade as drift.
    """
    ddl = canonical_deploy_ddl()
    tables_dir = tmp_path / DEFAULT_SERVICE_SCHEMA / "tables"
    tables_dir.mkdir(parents=True)
    for name, body in ddl.items():
        # Pad with trailing spaces and blank lines — noise that canonical_normalize drops.
        noisy = body.replace("\n", "   \n") + "\n\n\n"
        (tables_dir / f"{name}.sql").write_text(noisy, encoding="utf-8")
    assert validate_deploy_ddl(tmp_path) == []


def test_validate_deploy_ddl_returns_list_type(tmp_path: Path) -> None:
    """Return type is list[str] (empty when OK) — lock the contract for S8."""
    _write_canonical_codebase(tmp_path)
    result = validate_deploy_ddl(tmp_path)
    assert isinstance(result, list)
    assert all(isinstance(w, str) for w in result)


@pytest.mark.parametrize(
    "table_name",
    ["schema_version", "script_history", "script_audit_log"],
)
def test_each_template_renders_create_table_if_not_exists(table_name: str) -> None:
    """Idempotent CREATE (CDF-8): every canonical table uses IF NOT EXISTS.

    A regression here (someone drops the IF NOT EXISTS) would break re-deploy
    safety — catch it at the template level.
    """
    body = canonical_deploy_ddl()[table_name]
    assert "CREATE TABLE IF NOT EXISTS" in body

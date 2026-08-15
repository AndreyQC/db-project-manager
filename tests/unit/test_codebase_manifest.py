"""Unit tests for the codebase manifest (dbpm.manifest.json)."""

from __future__ import annotations

import json

import pytest

from db_project_manager.domain.diff import CodebaseManifest
from db_project_manager.infrastructure.config.codebase_manifest import (
    MANIFEST_FILENAME,
    ManifestError,
    read_manifest,
    write_manifest,
)


def _manifest(**overrides) -> CodebaseManifest:
    base = {
        "db_type": "postgres",
        "database": "mydb",
        "generated_at": "2026-07-28T00:00:00+00:00",
        "tool_version": "0.1.0",
        "source_version": "2026.08.11.01",
    }
    base.update(overrides)
    return CodebaseManifest(**base)


def test_write_then_read_roundtrip(tmp_path):
    m = _manifest(db_type="greenplum", database="gpdb")
    written = write_manifest(m, tmp_path)
    assert written == tmp_path / MANIFEST_FILENAME
    assert written.is_file()

    restored = read_manifest(tmp_path)
    assert restored.db_type == "greenplum"
    assert restored.database == "gpdb"
    assert restored.format_version == 2
    assert restored.source_version == "2026.08.11.01"


def test_write_creates_codebase_root_if_missing(tmp_path):
    nested = tmp_path / "a" / "b"  # does not exist yet
    written = write_manifest(_manifest(), nested)
    assert written.is_file()
    assert read_manifest(nested).db_type == "postgres"


def test_write_is_atomic_no_tmp_left(tmp_path):
    write_manifest(_manifest(), tmp_path)
    # No leftover .tmp file
    leftover = list(tmp_path.glob("*.tmp"))
    assert leftover == []


def test_write_pretty_prints_json(tmp_path):
    write_manifest(_manifest(), tmp_path)
    text = (tmp_path / MANIFEST_FILENAME).read_text(encoding="utf-8")
    # pretty-printed: indent=2 produces newlines between keys
    assert "\n" in text
    # parseable
    json.loads(text)


def test_read_missing_manifest_raises(tmp_path):
    with pytest.raises(ManifestError, match="не содержит"):
        read_manifest(tmp_path)


def test_read_corrupt_json_raises(tmp_path):
    (tmp_path / MANIFEST_FILENAME).write_text("{not valid json", encoding="utf-8")
    with pytest.raises(ManifestError, match="повреждён"):
        read_manifest(tmp_path)


def test_read_unknown_db_type_raises(tmp_path):
    payload = {
        "db_type": "mysql",
        "database": "x",
        "generated_at": "2026-07-28T00:00:00+00:00",
        "format_version": 1,
    }
    (tmp_path / MANIFEST_FILENAME).write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ManifestError, match="Неизвестный тип БД"):
        read_manifest(tmp_path)


def test_read_validation_error_raises(tmp_path):
    # missing required field (database)
    payload = {"db_type": "postgres", "generated_at": "2026-07-28T00:00:00+00:00"}
    (tmp_path / MANIFEST_FILENAME).write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ManifestError, match="невалиден"):
        read_manifest(tmp_path)


# ----------------------------------------------------- Phase 10 (S2)


def test_write_requires_source_version(tmp_path):
    # source_version removed → write_manifest must refuse.
    m = _manifest(source_version="")
    with pytest.raises(ManifestError, match="не содержит 'source_version'"):
        write_manifest(m, tmp_path)


def test_write_rejects_invalid_calver(tmp_path):
    m = _manifest(source_version="2026.08.11")  # missing release number
    with pytest.raises(ManifestError, match="невалидный source_version"):
        write_manifest(m, tmp_path)


def test_read_v2_missing_source_version_raises(tmp_path):
    payload = {
        "db_type": "postgres",
        "database": "x",
        "generated_at": "2026-07-28T00:00:00+00:00",
        "format_version": 2,
        # no source_version
    }
    (tmp_path / MANIFEST_FILENAME).write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ManifestError, match="не содержит 'source_version'"):
        read_manifest(tmp_path)


def test_read_v2_invalid_calver_raises(tmp_path):
    payload = {
        "db_type": "postgres",
        "database": "x",
        "generated_at": "2026-07-28T00:00:00+00:00",
        "format_version": 2,
        "source_version": "2026-13-99",  # not calver
    }
    (tmp_path / MANIFEST_FILENAME).write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ManifestError, match="невалидный source_version"):
        read_manifest(tmp_path)


def test_read_v1_without_source_version_still_parses(tmp_path):
    """Legacy v1 manifest (no source_version) must parse so RE can rewrite it as v2.

    The migration path: read v1 → seed source_version → write as v2 (Phase 10 S6).
    If read_manifest refused v1 outright, the migration would be impossible.
    """
    payload = {
        "db_type": "postgres",
        "database": "legacy_db",
        "generated_at": "2026-06-01T00:00:00+00:00",
        "format_version": 1,
    }
    (tmp_path / MANIFEST_FILENAME).write_text(json.dumps(payload), encoding="utf-8")
    restored = read_manifest(tmp_path)
    assert restored.format_version == 1
    assert restored.source_version == ""  # empty default; v2 enforcement skipped on v1


def test_write_always_emits_format_version_2(tmp_path):
    # Even if input model has format_version=1, write_manifest canonicalizes to v2.
    m = _manifest(format_version=1)
    write_manifest(m, tmp_path)
    payload = json.loads((tmp_path / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    assert payload["format_version"] == 2
    assert payload["source_version"] == "2026.08.11.01"

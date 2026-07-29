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
    assert restored.format_version == 1


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

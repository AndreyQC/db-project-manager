"""Codebase manifest (``dbpm.manifest.json``) — whole-database properties.

A reverse-engineer tree (``<output>/<database>/``) is accompanied by a small JSON
manifest in its root that records properties of the *source database as a whole*:
its type (postgres/greenplum), name, and generation timestamp. This is distinct
from the per-object autodoc YAML header: autodoc = one object, manifest = the
entire DB. The compare feature (Phase 9) reads the manifest to check db_type
compatibility between two sides without a live connection.

Layout::

    {
      "db_type": "postgres",
      "database": "bookings_demo",
      "generated_at": "2026-07-28T12:34:56+00:00",
      "tool_version": "0.1.0",
      "format_version": 1
    }

No secrets — only type, name, timestamps. Safe to commit alongside the codebase.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from pydantic import ValidationError

from db_project_manager.domain.connection import SUPPORTED_DB_TYPES
from db_project_manager.domain.diff import CodebaseManifest

MANIFEST_FILENAME = "dbpm.manifest.json"
MANIFEST_FORMAT_VERSION = 1


class ManifestError(Exception):
    """Raised on missing, corrupt or unparseable manifest."""


def write_manifest(manifest: CodebaseManifest, codebase_root: str | Path) -> Path:
    """Write the manifest atomically into ``codebase_root``.

    Returns the path written. Uses a temp file + ``os.replace`` so a partial write
    never leaves a corrupt manifest behind (same pattern as ``GuiSettingsStore``).
    """
    root = Path(codebase_root)
    root.mkdir(parents=True, exist_ok=True)
    target = root / MANIFEST_FILENAME
    tmp_path = target.with_name(target.name + ".tmp")
    payload = json.loads(manifest.model_dump_json())
    payload["format_version"] = MANIFEST_FORMAT_VERSION
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, target)
    return target


def read_manifest(codebase_root: str | Path) -> CodebaseManifest:
    """Read and validate the manifest from ``codebase_root``.

    Raises :class:`ManifestError` with a human-readable message when the file is
    missing, not valid JSON, fails pydantic validation, or carries an unknown
    ``db_type``.
    """
    root = Path(codebase_root)
    path = root / MANIFEST_FILENAME
    if not path.is_file():
        raise ManifestError(
            f"Каталог '{root}' не содержит '{MANIFEST_FILENAME}'. "
            f"Выполните повторный reverse-engineer — теперь он сохраняет тип БД."
        )
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise ManifestError(f"Манифест '{path}' повреждён (невалидный JSON): {e}") from e

    try:
        manifest = CodebaseManifest.model_validate(data)
    except ValidationError as e:
        raise ManifestError(f"Манифест '{path}' невалиден: {e}") from e

    if manifest.db_type not in SUPPORTED_DB_TYPES:
        raise ManifestError(
            f"Неизвестный тип БД в манифесте '{path}': '{manifest.db_type}'. "
            f"Ожидается один из: {', '.join(SUPPORTED_DB_TYPES)}."
        )
    return manifest


def tool_version() -> str:
    """Return the installed db-pm package version, or '' if unavailable."""
    try:
        from importlib.metadata import version

        return version("db-project-manager")
    except Exception:  # noqa: BLE001 — metadata may be unavailable in dev/standalone
        return ""

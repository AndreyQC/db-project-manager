"""Generate a YamlProject from a directory of SQL files (Phase 13, S2).

Walk the source directory, parse each ``*.sql`` file, and assemble the objects
into a :class:`~db_project_manager.domain.yaml_project.YamlProject`.

Files in ``.dbm_graph/``, ``__migrations/``, ``__deploy/``, ``.git/`` and
``__pycache__/`` are skipped.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from db_project_manager.domain.yaml_project import YamlProject
from db_project_manager.infrastructure.sql.autodoc import extract_header, strip_autodoc
from db_project_manager.infrastructure.yaml_project.autodoc_parser import (
    build_yaml_schema,
    parse_autodoc_object,
    parse_sql_object,
)


#: Directories to skip during traversal.
_SKIP_DIRS = frozenset(
    {".dbm_graph", "__migrations", "__deploy", ".git", "__pycache__", ".venv", "node_modules"}
)

#: Root directory name that is treated as the database name when autodoc catalog is absent.
#: If the directory structure is <db>/<schema>/... (as produced by reverse-engineer),
#: we use the top-level directory name as the database name.
_DB_NAME_FROM_DIR: str | None = None  # resolved at generate time


def generate_yaml_project(
    source_dir: Path,
    db_type: str,
    source_version: str = "",
) -> YamlProject:
    """Build a YamlProject from a directory of SQL files.

    Args:
        source_dir: root of the codebase tree (as produced by ``db-pm reverse-engineer``).
        db_type: ``greenplum`` or ``postgres`` — stored in the project and validated
            when applying to a target.
        source_version: optional calver string (e.g. ``2026.08.27.01``); stored as-is.

    Returns:
        A fully-populated ``YamlProject`` with all objects grouped into schemas.

    The database name is taken from the autodoc header of the first parsed file
    (``object_catalog``). If no file has an autodoc header, the name of the
    *source_dir* is used as a fallback.
    """
    if not source_dir.is_dir():
        raise YamlGeneratorError(f"Source directory not found: {source_dir}")

    schema_objects: dict[str, list] = {}  # schema name -> domain objects
    db_name: str | None = None
    sql_files = _iter_sql_files(source_dir)

    for file_path in sql_files:
        sql_text = file_path.read_text(encoding="utf-8-sig")
        header = extract_header(sql_text)
        body = strip_autodoc(sql_text) if header else sql_text

        if header:
            obj = parse_autodoc_object(header, body, db_type)
            # Extract database name from first available autodoc header
            if db_name is None:
                db_name = header.get("object", {}).get("object_catalog") or ""
            schema_name = header.get("object", {}).get("object_schema") or "public"
        else:
            obj = parse_sql_object(body, db_type)
            if db_name is None:
                db_name = source_dir.name

        if obj is None:
            continue

        schema_objects.setdefault(schema_name if header else "public", []).append(obj)

    if not db_name:
        db_name = source_dir.name

    schemas = [
        build_yaml_schema(name, objs)
        for name, objs in sorted(schema_objects.items())
    ]

    return YamlProject(
        db_type=db_type,
        database=db_name,
        generated_at=datetime.now(timezone.utc).isoformat(),
        source_version=source_version,
        schemas=schemas,
    )


def _iter_sql_files(root: Path) -> list[Path]:
    """Walk ``root`` and yield all ``*.sql`` files, skipping hidden/system directories."""
    result: list[Path] = []
    for path in root.rglob("*.sql"):
        # Skip files inside skipped directories
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        result.append(path)
    return sorted(result)


class YamlGeneratorError(Exception):
    """Raised when a YamlProject cannot be generated from a directory."""

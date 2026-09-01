"""Generate a YamlProject from a directory of SQL files (Phase 13, S2).

Walk the source directory, parse each ``*.sql`` file, and assemble the objects
into a :class:`~db_project_manager.domain.yaml_project.YamlProject`.

Files in ``.dbm_graph/``, ``__migrations/``, ``__deploy/``, ``.git/`` and
``__pycache__/`` are skipped.

Files WITHOUT an autodoc header are parsed via the SQL fallback parser with
reduced fidelity (schema derived from the qualified DDL name, autodoc metadata
lost). They are reported in a warning at the end of the run — the recommended
fix is to ADD an autodoc header for real objects or REMOVE the file if it is
an artifact (deleting a real object's file silently drops it from the YAML).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

from db_project_manager.domain.yaml_project import YamlProject
from db_project_manager.infrastructure.sql.autodoc import (
    MARKER_OPEN,
    build_metadata,
    extract_header,
    replace_header_yaml,
    salvage_header_fields,
    strip_autodoc,
)
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
    require_autodoc: bool = False,
    fix_broken_autodoc: bool = False,
) -> YamlProject:
    """Build a YamlProject from a directory of SQL files.

    Args:
        source_dir: root of the codebase tree (as produced by ``db-pm reverse-engineer``).
        db_type: ``greenplum`` or ``postgres`` — stored in the project and validated
            when applying to a target.
        source_version: optional calver string (e.g. ``2026.08.27.01``); stored as-is.
        require_autodoc: fail with :class:`YamlGeneratorError` when any ``*.sql``
            file lacks an autodoc header (strict mode for CI pipelines). By default
            such files are parsed via the SQL fallback and reported in a warning.
        fix_broken_autodoc: rewrite headers whose YAML does not parse IN PLACE —
            a fresh autodoc is regenerated from the identity salvaged out of the
            broken block (extra sections like remarks are dropped, reported).
            Files are only touched when this flag is set; otherwise generation
            is read-only.

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
    no_autodoc_files: list[Path] = []
    broken_autodoc_files: list[Path] = []
    fixed_autodoc_files: list[Path] = []
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
            if MARKER_OPEN in sql_text:
                # Broken header: markers present, YAML does not parse. Identity
                # lines are individually valid — salvage them and REGENERATE a
                # fresh autodoc from scratch instead of dropping the file.
                broken_autodoc_files.append(file_path)
                salvage = salvage_header_fields(sql_text)
                if salvage.get("object_type") and salvage.get("object_name"):
                    header = build_metadata(
                        object_catalog=salvage.get("object_catalog") or source_dir.name,
                        object_schema=salvage.get("object_schema") or None,
                        object_type=salvage["object_type"],
                        object_name=salvage["object_name"],
                    )
                    body = strip_autodoc(sql_text)
                    obj = parse_autodoc_object(header, body, db_type)
                    schema_name = header.get("object", {}).get("object_schema") or "public"
                    if db_name is None:
                        db_name = salvage.get("object_catalog") or ""
                    if fix_broken_autodoc:
                        file_path.write_text(
                            replace_header_yaml(sql_text, header), encoding="utf-8"
                        )
                        fixed_autodoc_files.append(file_path)
                else:
                    # Salvage failed — degrade to the SQL fallback parser
                    parsed = parse_sql_object(strip_autodoc(sql_text), db_type)
                    if db_name is None:
                        db_name = source_dir.name
                    if parsed is None:
                        continue
                    obj = parsed.obj
                    schema_name = parsed.schema or "public"
            else:
                no_autodoc_files.append(file_path)
                parsed = parse_sql_object(body, db_type)
                if db_name is None:
                    db_name = source_dir.name
                if parsed is None:
                    continue
                obj = parsed.obj
                # Schema from the qualified DDL name; bare names land in public
                schema_name = parsed.schema or "public"

        if obj is None:
            continue

        schema_objects.setdefault(schema_name, []).append(obj)

    if no_autodoc_files or broken_autodoc_files:
        _report_missing_autodoc(
            no_autodoc_files, broken_autodoc_files, fixed_autodoc_files,
            source_dir, require_autodoc,
        )

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


def _report_missing_autodoc(
    no_autodoc_files: list[Path],
    broken_autodoc_files: list[Path],
    fixed_autodoc_files: list[Path],
    source_dir: Path,
    require_autodoc: bool,
) -> None:
    """Report files without a parsable autodoc header.

    Three distinct situations, three distinct messages:
    - no header: add one (or delete the file if it is an artifact — deleting a
      real object's file silently drops it from the YAML);
    - broken header: identity was salvaged and a fresh autodoc regenerated
      in-memory (extra sections like remarks are dropped for this run);
    - fixed files (``--fix-broken-autodoc``): rewritten on disk.
    Strict mode (``--require-autodoc``) fails on missing AND broken headers.
    """

    def _rel(files: list[Path]) -> str:
        return "\n".join(f"  - {p.relative_to(source_dir)}" for p in files)

    if require_autodoc:
        parts = []
        if no_autodoc_files:
            parts.append(
                f"{len(no_autodoc_files)} файл(ов) без autodoc-заголовка:\n{_rel(no_autodoc_files)}"
            )
        if broken_autodoc_files:
            parts.append(
                f"{len(broken_autodoc_files)} файл(ов) с непарсящимся autodoc-заголовком "
                f"(невалидный YAML):\n{_rel(broken_autodoc_files)}"
            )
        raise YamlGeneratorError(
            "Режим --require-autodoc: " + ";\n".join(parts) + "\n"
            "Добавьте/исправьте autodoc-заголовки (см. --fix-broken-autodoc) "
            "или удалите файлы, если это артефакты."
        )

    if no_autodoc_files:
        logger.warning(
            f"{len(no_autodoc_files)} файл(ов) без autodoc-заголовка разобрано через "
            f"SQL-fallback (схема из qualified имени, метаданные autodoc недоступны):\n"
            f"{_rel(no_autodoc_files)}\n"
            "Рекомендуется добавить autodoc-заголовки; если файлы — временные "
            "артефакты, удалите их (реальный объект при удалении файла исчезнет из YAML)."
        )
    if broken_autodoc_files:
        if fixed_autodoc_files:
            logger.warning(
                f"{len(fixed_autodoc_files)} файл(ов) с невалидным YAML в autodoc-заголовке: "
                f"заголовки ПЕРЕГЕНЕРИРОВАНЫ на диске из salvage-identity "
                f"(дополнительные секции вроде remarks/migration_to_pg удалены):\n"
                f"{_rel(fixed_autodoc_files)}"
            )
        else:
            logger.warning(
                f"{len(broken_autodoc_files)} файл(ов) с невалидным YAML в autodoc-заголовке: "
                f"identity восстановлена из заголовка, autodoc пересобран в памяти "
                f"(файлы НЕ изменялись; используйте --fix-broken-autodoc для записи):\n"
                f"{_rel(broken_autodoc_files)}"
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

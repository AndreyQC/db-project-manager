"""Canonical DDL for the ``__deploy`` service schema (Phase 10, S5).

db-pm owns the structure of the service schema: reverse-engineer seeds it,
deploy applies it from the codebase, and this module is the single source of
truth for what "correct" looks like. A SHA-256 mismatch between the canonical
DDL and what's in the codebase triggers a *warning* (CDF-10 approach b) — never
a hard block (MVP, the warning may be promoted in backlog if it proves weak).

The checksum is computed on the executable SQL body only:
``script_checksum(strip_autodoc(raw))`` — so a metadata-only change to the
autodoc header (e.g. db-pm adds an informational comment) does NOT raise a
false positive (CDF-6).

Used by:
* deploy (S8) — emits a warning when the codebase's __deploy differs from
  canonical, before applying it on the temp DB.
* reverse-engineer (S6) — when seeding, writes the canonical DDL rendered
  from the same templates (so seed always matches canonical by construction).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from db_project_manager.domain.deploy import canonical_normalize, script_checksum
from db_project_manager.infrastructure.sql.autodoc import ensure_header, strip_autodoc

DEFAULT_SERVICE_SCHEMA = "__deploy"

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates" / "deploy"
_TABLES: tuple[str, ...] = ("schema_version", "script_history", "script_audit_log")


def seed_deploy_files(
    deploy_dir: Path,
    service_schema: str,
    db_name: str,
    *,
    overwrite: bool = False,
) -> list[Path]:
    """Write the canonical service-schema tree (schema + 3 tables) on disk.

    Shared by reverse-engineer (Phase 10 S6) and ``yaml apply`` (Phase 13
    feedback 01.09: a YAML-produced codebase must pass ``_validate_deploy_presence``
    without going through RE). Each file is decorated with the ``immutable``
    autodoc marker (CDF-10 — objects managed by db-pm).

    Args:
        deploy_dir: ``<codebase>/<service_schema>`` directory (created if needed).
        service_schema: service schema name (configurable, default ``__deploy``).
        db_name: database name for the autodoc ``object_catalog``.
        overwrite: rewrite files even when present. RE passes True (seeding is
            idempotent-by-canonical); yaml apply passes False so a re-apply into
            an existing codebase never clobbers files already there.

    Returns:
        The list of files actually written.
    """
    written: list[Path] = []
    deploy_dir.mkdir(parents=True, exist_ok=True)

    schema_file = deploy_dir / f"schema {service_schema}.sql"
    if overwrite or not schema_file.is_file():
        schema_body = f'CREATE SCHEMA IF NOT EXISTS "{service_schema}";\n'
        schema_file.write_text(
            ensure_header(
                schema_body,
                object_catalog=db_name,
                object_schema=service_schema,
                object_type="schema",
                object_name=service_schema,
                immutable=True,
            ),
            encoding="utf-8",
        )
        written.append(schema_file)

    tables_dir = deploy_dir / "tables"
    tables_dir.mkdir(parents=True, exist_ok=True)
    for table_name, body in canonical_deploy_ddl(service_schema).items():
        table_file = tables_dir / f"{table_name}.sql"
        if overwrite or not table_file.is_file():
            table_file.write_text(
                ensure_header(
                    body,
                    object_catalog=db_name,
                    object_schema=service_schema,
                    object_type="table",
                    object_name=table_name,
                    immutable=True,
                ),
                encoding="utf-8",
            )
            written.append(table_file)
    return written


@lru_cache(maxsize=1)
def _env() -> Environment:
    """Jinja environment loading only the deploy templates.

    Mirrors the generator's settings (trim_blocks/lstrip_blocks) so the rendered
    output is deterministic and whitespace-stable — important because the
    checksum is whitespace-sensitive after canonical_normalize (which strips
    trailing whitespace per line but preserves internal structure).
    """
    return Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )


@lru_cache(maxsize=4)
def canonical_deploy_ddl(schema_name: str = DEFAULT_SERVICE_SCHEMA) -> dict[str, str]:
    """Return canonical DDL for the three __deploy tables, keyed by table name.

    Rendered from the deploy templates with the given ``schema_name``. The
    returned strings are the *body* (no autodoc header) — callers that write
    files prepend the header via :func:`ensure_header` (Phase 10 S6).
    """
    env = _env()
    return {name: env.get_template(f"{name}.sql.j2").render(schema=schema_name) for name in _TABLES}


@lru_cache(maxsize=4)
def canonical_deploy_checksums(schema_name: str = DEFAULT_SERVICE_SCHEMA) -> dict[str, str]:
    """Return SHA-256 of each canonical table's DDL (after canonical_normalize).

    The canonical DDL has no autodoc header, so strip_autodoc is a no-op here —
    but we still call it for symmetry with :func:`validate_deploy_ddl` (where
    the input does carry a header). The two sides of the comparison must use
    the exact same normalization pipeline.
    """
    return {
        name: script_checksum(strip_autodoc(ddl))
        for name, ddl in canonical_deploy_ddl(schema_name).items()
    }


def validate_deploy_ddl(
    codebase_dir: str | Path, schema_name: str = DEFAULT_SERVICE_SCHEMA
) -> list[str]:
    """Compare codebase's ``<schema>/tables/*.sql`` to the canonical DDL.

    Returns a list of warning strings (empty = match). For each table:
      * missing file → warning;
      * SHA-256 mismatch → warning with both hashes (truncated).

    Checksum is computed on ``strip_autodoc(canonical_normalize(raw))`` so a
    metadata-only autodoc change does NOT trigger a false positive (CDF-6).
    A warning never blocks deploy (CDF-10 approach b).
    """
    codebase_dir = Path(codebase_dir)
    expected = canonical_deploy_checksums(schema_name)
    tables_dir = codebase_dir / schema_name / "tables"
    warnings: list[str] = []
    for table_name, expected_hash in expected.items():
        path = tables_dir / f"{table_name}.sql"
        if not path.is_file():
            warnings.append(
                f"{schema_name}/tables/{table_name}.sql отсутствует — "
                f"canonical-deploy схема в кодовой базе неполная."
            )
            continue
        raw = path.read_text(encoding="utf-8-sig")
        actual_hash = script_checksum(strip_autodoc(raw))
        if actual_hash != expected_hash:
            warnings.append(
                f"{schema_name}/tables/{table_name}.sql отличается от canonical DDL "
                f"(ожидался SHA-256 {expected_hash[:8]}…, получен {actual_hash[:8]}…). "
                f"Файл управляется db-pm — не редактируйте вручную; "
                f"запустите reverse-engineer для регенерации."
            )
    return warnings


# Re-export canonical_normalize so callers importing from this module have a
# single place to reach the canonical-text helpers (avoids spreading imports
# across deploy-service and reverse-engineer). Noop at runtime.
__all__ = [
    "DEFAULT_SERVICE_SCHEMA",
    "canonical_deploy_checksums",
    "canonical_deploy_ddl",
    "canonical_normalize",
    "seed_deploy_files",
    "validate_deploy_ddl",
]

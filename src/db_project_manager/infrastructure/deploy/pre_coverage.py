"""Static pre-script coverage extraction (Phase 11, SG-3).

The safety gate never executes pre-scripts (``deploy analyze`` is a dry-run);
it only needs to know **which tables each pre-script claims to handle**. A
script declares that explicitly in its autodoc ``project`` section:

```
/*[[autodoc-yaml]
object:
  ...
project:
  build: true
  covers:
    - app.orders
    - app.items
[autodoc-yaml]>]*/
```

Coverage is an author's claim — the gate checks the declaration, not the
script semantics (SG-3: explicitness over magic). Broken declarations are
skipped with a warning instead of failing the parse (LESSONS §36: YAML
barewords such as ``YES``/``NO`` parse into bools — only plain strings are
accepted).
"""

from __future__ import annotations

from pathlib import Path

from loguru import logger

from db_project_manager.infrastructure.sql.autodoc import extract_header


def read_pre_coverage(migrations_dir: Path) -> dict[tuple[str, str], list[str]]:
    """Collect table coverage declared by pre-scripts (SG-3).

    Reads ``<migrations_dir>/pre/*.sql`` in sorted order (deterministic
    ``covered_by`` lists) and extracts ``project.covers`` from each script's
    autodoc header. Returns ``{(schema, table): [script_name, ...]}``.

    A missing or empty ``pre/`` directory is not an error — no scripts means
    no coverage (the gate then flags every data-bearing touched table).
    Scripts without an autodoc header or without a ``covers`` list contribute
    nothing; individual broken entries are skipped with a warning.
    """
    pre_dir = migrations_dir / "pre"
    if not pre_dir.is_dir():
        return {}
    coverage: dict[tuple[str, str], list[str]] = {}
    for script_path in sorted(pre_dir.glob("*.sql")):
        meta = extract_header(script_path.read_text(encoding="utf-8"))
        if not isinstance(meta, dict):
            continue
        covers = (meta.get("project") or {}).get("covers")
        if not isinstance(covers, list):
            continue
        for entry in covers:
            key = _normalize_cover_entry(entry, script_path.name)
            if key is None:
                continue
            coverage.setdefault(key, []).append(script_path.name)
    return coverage


def _normalize_cover_entry(entry: object, script_name: str) -> tuple[str, str] | None:
    """Normalize one ``covers`` entry to a ``(schema, table)`` tuple, or warn+skip."""
    if not isinstance(entry, str):
        logger.warning(
            f"pre-script {script_name!r}: covers entry {entry!r} is not a string — skipped"
        )
        return None
    parts = entry.strip().split(".", 1)
    if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
        logger.warning(
            f"pre-script {script_name!r}: invalid covers entry {entry!r} "
            "(expected 'schema.table') — skipped"
        )
        return None
    return (parts[0].strip(), parts[1].strip())

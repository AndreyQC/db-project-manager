"""Autodoc YAML header for generated SQL files.

A generated SQL file may start with a structured comment block that carries
object metadata as YAML, so that downstream tooling (parser, dependency graph,
diff) can read object identity without re-parsing SQL:

    /*====================================================================================
    [<[autodoc-yaml]]
    object:
      object_catalog: <database>
      object_schema: <schema>
      object_type: <table|view|...>
      object_name: <name>
      object_key: pg_database/<catalog>/schema/<schema>/type/<type>/name/<name>
    project:
      build: true
    [[autodoc-yaml]>]
    ====================================================================================*/

Format mirrors the POC (database_service.py) so existing tooling stays compatible.
The block is optional; when absent, render_header() produces one to prepend.
"""

from __future__ import annotations

from typing import Any

import yaml

# Block markers (kept identical to the POC for compatibility).
MARKER_OPEN = "[<[autodoc-yaml]]"
MARKER_CLOSE = "[[autodoc-yaml]>]"

_TEMPLATE = """\
/*====================================================================================
[<[autodoc-yaml]]
{yaml}[[autodoc-yaml]>]
=====================================================================================*/

"""


def build_metadata(
    *,
    object_catalog: str,
    object_schema: str | None,
    object_type: str,
    object_name: str,
) -> dict[str, Any]:
    """Build the autodoc metadata dict for an object."""
    object_key = _build_object_key(
        object_catalog=object_catalog, object_schema=object_schema, object_type=object_type, object_name=object_name
    )
    return {
        "object": {
            "object_catalog": object_catalog,
            "object_schema": object_schema,
            "object_type": object_type,
            "object_name": object_name,
            "object_key": object_key,
        },
        "project": {"build": True},
    }


def _build_object_key(
    *,
    object_catalog: str,
    object_schema: str | None,
    object_type: str,
    object_name: str,
) -> str:
    schema_part = f"schema/{object_schema}/" if object_schema else ""
    return f"pg_database/{object_catalog}/{schema_part}type/{object_type}/name/{object_name}"


def render_header(metadata: dict[str, Any]) -> str:
    """Render the autodoc comment block (markers + YAML) for prepending."""
    body = yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False)
    return _TEMPLATE.format(yaml=body)


def extract_header(script: str) -> dict[str, Any] | None:
    """Parse an existing autodoc header from a script; None if absent.

    Returns the parsed YAML dict (metadata) on success.
    """
    if MARKER_OPEN not in script or MARKER_CLOSE not in script:
        return None
    start = script.index(MARKER_OPEN) + len(MARKER_OPEN)
    end = script.index(MARKER_CLOSE)
    body = script[start:end]
    try:
        parsed = yaml.safe_load(body)
    except yaml.YAMLError:
        return None
    return parsed if isinstance(parsed, dict) else None


def ensure_header(
    script: str,
    *,
    object_catalog: str,
    object_schema: str | None,
    object_type: str,
    object_name: str,
) -> str:
    """Prepend an autodoc header if absent; keep an existing one as-is.

    Use this to decorate freshly rendered SQL bodies.
    """
    if MARKER_OPEN in script and MARKER_CLOSE in script:
        return script
    metadata = build_metadata(
        object_catalog=object_catalog,
        object_schema=object_schema,
        object_type=object_type,
        object_name=object_name,
    )
    return render_header(metadata) + script

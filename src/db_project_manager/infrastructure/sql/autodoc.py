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

from collections.abc import Callable
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
    object_signature: str = "",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the autodoc metadata dict for an object.

    Args:
        object_signature: Canonical signature hash (8 hex chars) for overloaded
            functions/procedures. Empty for non-overloaded objects — in that
            case the field is omitted from the metadata entirely so table/view
            autodoc headers stay clean.
        extra: Optional additional keys merged into the ``object`` mapping.
            Phase 5 uses this for ``extension_version`` (informational, the
            installed version on the source DB) and ``properties`` (db-level
            CREATE DATABASE properties carried by the ``database_setting``
            object). Keys must not collide with the standard object fields.
    """
    object_key = _build_object_key(
        object_catalog=object_catalog,
        object_schema=object_schema,
        object_type=object_type,
        object_name=object_name,
        object_signature=object_signature,
    )
    obj = {
        "object_catalog": object_catalog,
        "object_schema": object_schema,
        "object_type": object_type,
        "object_name": object_name,
        "object_key": object_key,
    }
    if object_signature:
        obj["object_signature"] = object_signature
    if extra:
        collision = set(extra) & set(obj)
        if collision:
            raise ValueError(f"extra keys collide with standard fields: {collision}")
        obj.update(extra)
    return {"object": obj, "project": {"build": True}}


def _build_object_key(
    *,
    object_catalog: str,
    object_schema: str | None,
    object_type: str,
    object_name: str,
    object_signature: str = "",
) -> str:
    """Build the object_key. For overloaded functions/procedures (non-empty
    ``object_signature``) a ``/signature/<hash>`` suffix is appended so each
    overload gets a distinct key. For all other objects the format is unchanged
    (backward-compatible with existing ``.dbm_graph/`` stores).
    """
    schema_part = f"schema/{object_schema}/" if object_schema else ""
    key = f"pg_database/{object_catalog}/{schema_part}type/{object_type}/name/{object_name}"
    if object_signature:
        key += f"/signature/{object_signature}"
    return key


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


def strip_autodoc(script: str) -> str:
    """Remove the leading autodoc comment block, return the executable SQL body.

    Finds the closing marker ``[[autodoc-yaml]>]`` plus the trailing ``*/`` that
    closes the surrounding SQL comment, and returns whatever follows. If no
    autodoc block is present, the script is returned unchanged.

    Used both at deploy time (before ``execute_script`` — metadata must not leak
    into the target DB) and at checksum time (Phase 10: ``canonical_normalize``
    strips the autodoc so the checksum reflects executable SQL, not metadata —
    a metadata-only change does not invalidate the checksum).
    """
    if MARKER_CLOSE not in script:
        return script
    end = script.index(MARKER_CLOSE) + len(MARKER_CLOSE)
    tail = script[end:]
    # Drop the comment-closing '*/' if present.
    comment_end = tail.find("*/")
    if comment_end != -1:
        tail = tail[comment_end + 2 :]
    return tail.lstrip()


def ensure_header(
    script: str,
    *,
    object_catalog: str,
    object_schema: str | None,
    object_type: str,
    object_name: str,
    object_signature: str = "",
    extra: dict[str, Any] | None = None,
) -> str:
    """Prepend an autodoc header if absent; keep an existing one as-is.

    Use this to decorate freshly rendered SQL bodies.

    Args:
        object_signature: Canonical signature hash for overloaded functions/
            procedures. See :func:`build_metadata`.
        extra: Optional additional object fields. See :func:`build_metadata`.
    """
    if MARKER_OPEN in script and MARKER_CLOSE in script:
        return script
    metadata = build_metadata(
        object_catalog=object_catalog,
        object_schema=object_schema,
        object_type=object_type,
        object_name=object_name,
        object_signature=object_signature,
        extra=extra,
    )
    return render_header(metadata) + script


def update_header(script: str, mutator: Callable[[dict[str, Any]], None]) -> str:
    """Apply *mutator* to the parsed autodoc metadata and re-render in place.

    Unlike :func:`ensure_header`, this MUTATES an existing header — it parses the
    YAML between the markers, lets *mutator* modify the dict in place, then writes
    the new YAML back between the same markers, preserving the surrounding
    comment block and the SQL body after it.

    No-op when no header is present (or YAML is unparseable).

    Used by the qualify-refs post-processor to record which bare identifiers
    were qualified, without re-rendering the whole file.
    """
    if MARKER_OPEN not in script or MARKER_CLOSE not in script:
        return script
    metadata = extract_header(script)
    if metadata is None:
        return script
    mutator(metadata)
    new_yaml = yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False)
    open_idx = script.index(MARKER_OPEN) + len(MARKER_OPEN)
    close_idx = script.index(MARKER_CLOSE)
    return script[:open_idx] + new_yaml + script[close_idx:]

"""Serializer for YamlProject: Pydantic model → YAML text and back.

Uses ``yaml.safe_dump`` / ``yaml.safe_load`` — the same approach as the existing
autodoc YAML handling in ``infrastructure/sql/autodoc.py``.
"""

from __future__ import annotations

import yaml
from typing import Any

from db_project_manager.domain.yaml_project import YamlProject


def serialize_yaml_project(project: YamlProject) -> str:
    """Render a YamlProject to a YAML string.

    Keys are emitted in model field-declaration order (NOT alphabetically):
    the entity name key (``schema_name`` / ``table_name`` / ...) comes FIRST
    in its block, so a reader sees *what* the object is before its columns —
    alphabetical sorting buried the name under hundreds of column lines.
    Declaration order is deterministic, so the file stays diff-friendly.
    """
    data = project.model_dump(mode="python", exclude_none=True, by_alias=True)
    return yaml.safe_dump(
        data,
        allow_unicode=True,
        sort_keys=False,
        width=200,  # avoid line-wrapping of long SQL strings
    )


def parse_yaml_project(yaml_text: str) -> YamlProject:
    """Parse a YAML string into a YamlProject.

    Raises:
        YamlProjectError: if the text is not valid YAML or fails Pydantic validation.
    """
    raw: dict[str, Any] = yaml.safe_load(yaml_text)
    if not isinstance(raw, dict):
        raise YamlProjectError("YAML project root must be a mapping.")
    return YamlProject.model_validate(raw)


class YamlProjectError(ValueError):
    """Raised when a YAML project file cannot be parsed or validated."""

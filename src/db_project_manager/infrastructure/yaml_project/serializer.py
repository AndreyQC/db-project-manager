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

    The output is sorted by key within each mapping so that the same project
    always produces a deterministic file (important for git diffs).
    """
    data = project.model_dump(mode="python", exclude_none=True)
    return yaml.safe_dump(
        data,
        allow_unicode=True,
        sort_keys=True,
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

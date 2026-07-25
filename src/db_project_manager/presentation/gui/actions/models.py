"""Pydantic models for per-action GUI settings.

Empty string means "not configured" — the panel substitutes defaults (selected
connection, cfg.paths.default_output_dir) and validates required_fields before
execution. extra="ignore" keeps old settings files forward-compatible.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ReverseEngineerSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    connection: str = ""
    output_dir: str = ""


class DeployValidateSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    codebase_dir: str = ""
    connection: str = ""
    prefix: str = ""
    keep_db: bool = False
    continue_on_error: bool = False


# Export format value meaning "build only, no export".
FORMAT_NONE = "none"
EXPORT_FORMATS = ("graphml", "json", "dot", FORMAT_NONE)


class GraphPrepareSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    codebase_dir: str = ""
    format: str = "graphml"  # graphml (Gephi) | json | dot | none
    # Named validate_graph: "validate" would shadow BaseModel.validate.
    validate_graph: bool = True

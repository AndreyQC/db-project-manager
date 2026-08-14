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
    # Optional export destination dir. Empty = <codebase>/.dbm_graph/.
    output_dir: str = ""


class CompareSettings(BaseModel):
    """Settings for the compare action (Phase 9 / GUI action).

    Two sides (source/target), each is either a DB connection or a reverse-engineer
    directory. Exactly one of ``<side>_connection`` / ``<side>_dir`` must be set per
    side — this XOR rule is enforced at build time (SideSpec construction / CLI),
    not by pydantic, because the panel leaves both empty until the user configures.
    """

    model_config = ConfigDict(extra="ignore")

    source_connection: str = ""
    source_dir: str = ""
    target_connection: str = ""
    target_dir: str = ""
    # Report destination (source.json / target.json / diff_report.json).
    output_dir: str = ""
    keep_model_dir: bool = False


class DeployAnalyzeSettings(BaseModel):
    """Settings for the deploy analyze action (Phase 11 / GUI action).

    Run-only safety gate against an EXISTING target DB (read-only — nothing
    is applied). Field named ``target_connection`` to avoid colliding with
    BaseModel attribute names (LESSONS §40) and to stress "existing target".
    """

    model_config = ConfigDict(extra="ignore")

    codebase_dir: str = ""
    target_connection: str = ""
    # Report destination (safety_gate_report.md / .json / diff_report.json).
    output_dir: str = ""

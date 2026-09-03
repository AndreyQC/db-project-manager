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


class YamlGenerateSettings(BaseModel):
    """Settings for 'db-pm yaml generate' (Phase 13 / GUI action).

    Walks a Greenplum/Postgres SQL directory and produces a portable YAML file.
    """

    model_config = ConfigDict(extra="ignore")

    source_dir: str = ""
    db_type: str = "greenplum"  # greenplum | postgres
    output_file: str = ""
    source_version: str = ""


class YamlApplySettings(BaseModel):
    """Settings for 'db-pm yaml apply' (Phase 13 / GUI action).

    Reads a YAML file and generates a full codebase (SQL + manifest + graph)
    for a target DB type. GP → PG transformation: external tables are skipped,
    DISTRIBUTED BY / WITH options are dropped.
    """

    model_config = ConfigDict(extra="ignore")

    yaml_file: str = ""
    target_db_type: str = "postgres"  # greenplum | postgres
    output_dir: str = ""


class DeployApplySettings(BaseModel):
    """Settings for the deploy plan/apply actions (Phase 15 / GUI action).

    Shared between two actions (``deploy_plan`` is dry-run; ``deploy_apply``
    mutates an EXISTING target DB). Field named ``target_connection`` to avoid
    colliding with BaseModel (LESSONS §40) and to stress "existing target".

    ``confirm_understands_risk`` is a GUI-side gate (LESSONS §43 + preflight
    pattern): the OK button is disabled until the user checks the box. The
    CLI builder ignores this flag — it is not part of the CLI contract.
    """

    model_config = ConfigDict(extra="ignore")

    codebase_dir: str = ""
    target_connection: str = ""
    output_dir: str = ""
    include_drops: bool = False
    no_rehearsal: bool = False  # CLI applies this only to deploy apply
    keep_rehearsal_db: bool = False  # CLI applies this only to deploy apply
    confirm_understands_risk: bool = False  # GUI gate, never sent to CLI


class DeployInitServiceSchemaSettings(BaseModel):
    """Settings for ``db-pm deploy init-service-schema`` (Phase 15.5.2 / GUI action).

    Idempotent bootstrap of the ``__deploy`` service schema on a target DB.
    Run this BEFORE the first ``deploy apply`` against a freshly created
    target database — otherwise apply silently skips ``__deploy`` (which
    CompareService flags UNCHANGED via RE seeding into a temp snapshot).
    """

    model_config = ConfigDict(extra="ignore")

    target_connection: str = ""

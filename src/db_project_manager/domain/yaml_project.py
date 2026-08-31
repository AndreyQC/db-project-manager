"""Domain models for the YAML-based project representation (Phase 13).

A ``YamlProject`` is a portable, human-readable snapshot of a database structure
that can be generated from a directory of SQL files (with or without autodoc
headers) and used to produce a full codebase (manifest + SQL files + graph).

The format is database-agnostic at the top level: ``db_type`` drives which
sub-fields are expected (``distributed_by`` / ``with_options`` for Greenplum;
absent for PostgreSQL).

Design decisions:
- ``db_type`` is a Literal so that Pydantic validates compatibility at parse
  time (``YAML`` written for GP with ``external_table`` must not be applied
  to a Postgres target without an error).
- Column types are stored as plain strings (the same canonical form produced
  by ``extract_columns`` in the diff layer — lower-cased, aliases collapsed).
- ``definition`` for views and functions is the raw SQL body; it is NOT
  re-normalized on parse (the serialized form is the source of truth).
- ``distributed_by`` / ``with_options`` are Greenplum-only and are placed in
  ``gp_options`` so that a Postgres target simply ignores them.
- ``object_key`` format follows the existing convention
  (``database/<db>/schema/<s>/type/<t>/name/<n>``) WITHOUT the catalog
  segment — consistent with the identity-key decision in LESSONS §51.
- Serialized entity keys are descriptive (``schema_name``, ``table_name``,
  ``column_name``, ``view_name``, ``function_name``, ``external_table_name``)
  instead of a generic ``name`` at every nesting level: in a long YAML file
  a bare ``name`` is ambiguous when scanning for errors. The Python attribute
  stays ``name`` (aliases are serialization-only); parsing accepts both the
  descriptive key and the legacy ``name``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


class YamlColumn(BaseModel):
    """One table/view/external-table column."""

    model_config = ConfigDict(extra="ignore")

    name: str = Field(
        serialization_alias="column_name",
        validation_alias=AliasChoices("column_name", "name"),
    )
    type: str
    nullable: bool = True
    default: str | None = None


class YamlTable(BaseModel):
    """A regular table (Postgres / Greenplum)."""

    model_config = ConfigDict(extra="ignore")

    name: str = Field(
        serialization_alias="table_name",
        validation_alias=AliasChoices("table_name", "name"),
    )
    columns: list[YamlColumn] = []
    distributed_by: list[str] = []  # Greenplum only; Postgres: empty
    with_options: dict[str, str] = {}  # Greenplum only; e.g. {appendoptimized: true, orientation: column}


class YamlExternalTable(BaseModel):
    """A writable external table (Greenplum only).

    Postgres does not support writable external tables — applying a YAML that
    contains these to a Postgres target produces a validation error.
    """

    model_config = ConfigDict(extra="ignore")

    name: str = Field(
        serialization_alias="external_table_name",
        validation_alias=AliasChoices("external_table_name", "name"),
    )
    columns: list[YamlColumn] = []
    location: str  # raw LOCATION clause content, e.g. "pxf://staging_tr.../?PROFILE=JDBC&SERVER=..."
    format_type: str = "CUSTOM"  # FORMAT 'CUSTOM' / 'TEXT' / 'CSV' etc.
    format_options: str = ""  # e.g. "FORMATTER='pxfwritable_export'"
    encoding: str = "UTF8"


class YamlView(BaseModel):
    """A view or materialized view."""

    model_config = ConfigDict(extra="ignore")

    name: str = Field(
        serialization_alias="view_name",
        validation_alias=AliasChoices("view_name", "name"),
    )
    columns: list[YamlColumn] = []
    definition: str  # full SELECT ... AS ... or CREATE MATERIALIZED VIEW ...
    is_materialized: bool = False


class YamlFunction(BaseModel):
    """A function or procedure."""

    model_config = ConfigDict(extra="ignore")

    name: str = Field(
        serialization_alias="function_name",
        validation_alias=AliasChoices("function_name", "name"),
    )
    arguments: list[dict[str, str]] = []  # [{name: p_x, type: text}, ...]; name may be empty
    returns: str = ""  # e.g. "json", "void", "TABLE(...)"
    definition: str  # full SQL body after AS $$
    language: str = "plpgsql"
    security_definer: bool = False
    is_trigger: bool = False


class YamlSchema(BaseModel):
    """All objects belonging to one schema."""

    model_config = ConfigDict(extra="ignore")

    name: str = Field(
        serialization_alias="schema_name",
        validation_alias=AliasChoices("schema_name", "name"),
    )
    tables: list[YamlTable] = []
    views: list[YamlView] = []
    functions: list[YamlFunction] = []
    external_tables: list[YamlExternalTable] = []  # Greenplum only


class YamlProject(BaseModel):
    """The root of a portable YAML project file.

    Produced by ``db-pm yaml generate`` from a directory of SQL files.
    Consumed by ``db-pm yaml apply`` to produce a full codebase with
    ``dbpm.manifest.json`` and ``.dbm_graph/``.
    """

    model_config = ConfigDict(extra="ignore")

    db_type: Literal["greenplum", "postgres"]
    database: str  # bare database name, e.g. "cis_zup"
    generated_at: str  # ISO-8601 UTC
    source_version: str = ""  # calver YYYY.MM.DD.NN; empty if not set
    schemas: list[YamlSchema] = []

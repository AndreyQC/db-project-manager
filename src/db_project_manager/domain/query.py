"""Domain models for query/EXPLAIN results (Phase 19, MCP server surface).

Engine-agnostic by design: ``ExplainResult.fmt`` is a free-form string so
future adapters can report their native plan shape (MSSQL ``xml``,
Snowflake ``tabular``, ...) without breaking the contract.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class QueryResult(BaseModel):
    """Result of a single read-only (or explicit write-mode) statement.

    Rows are column-name -> JSON-serializable value dicts; driver-specific
    types (Decimal, datetime, memoryview, UUID) are stringified by the
    adapter so an MCP client can serialize the model directly.
    """

    model_config = ConfigDict(extra="ignore")

    columns: list[str] = Field(default_factory=list, description="Column names in result order")
    rows: list[dict[str, Any]] = Field(default_factory=list, description="Row dicts, at most max_rows")
    row_count: int = Field(default=0, ge=0, description="Number of returned rows (after truncation)")
    total_hint: int | None = Field(
        default=None,
        description="Total rows the statement produced, when known without extra cost",
    )
    truncated: bool = Field(default=False, description="True when more rows existed beyond max_rows")
    duration_ms: int = Field(default=0, ge=0, description="Statement wall time")
    notices: list[str] = Field(
        default_factory=list,
        description="Non-fatal notes (missing read-only backstop, plan-format fallback, ...)",
    )


class ExplainResult(BaseModel):
    """Execution plan of a single statement.

    ``plan`` holds the native plan: text lines joined into one string
    (``fmt="text"``) or a parsed structure (``fmt="json"`` and engine
    equivalents — xml/tabular arrive as strings).
    """

    model_config = ConfigDict(extra="ignore")

    fmt: str = Field(description="Plan format: text | json | xml | tabular | ...")
    plan: str | dict[str, Any] = Field(description="Plan payload in the declared format")
    analyzed: bool = Field(default=False, description="Whether the statement was actually executed (EXPLAIN ANALYZE)")
    notices: list[str] = Field(default_factory=list)

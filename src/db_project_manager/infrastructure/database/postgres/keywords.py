"""PostgreSQL reserved keywords loader.

Loads ``keywords.yaml`` (PG 18) and exposes the reserved set for use by
the SQL generator.  This module is DBMS-specific — each supported database
(Snowflake, ClickHouse, MySQL, etc.) has its own ``keywords.yaml`` next to
its adapter.
"""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

import yaml

# Path to this file's directory — the YAML lives alongside it.
_YAML_PATH = Path(__file__).with_name("keywords.yaml")

#: Frozen set of PostgreSQL 18 reserved keywords (uppercase).
#: Identifiers matching this set must be double-quoted in DDL.
def _loadReserved() -> list[str]:
    """Load the reserved keyword list from keywords.yaml."""
    # Try package resource first, fall back to direct file path (useful in dev).
    try:
        data = yaml.safe_load(files(__package__).joinpath("keywords.yaml").read_text(encoding="utf-8"))
    except Exception:
        # Fallback: running from source without a package install.
        data = yaml.safe_load(_YAML_PATH.read_text(encoding="utf-8"))

    return data.get("reserved", [])


#: Frozen set of PostgreSQL 18 reserved keywords (uppercase).
#: Identifiers matching this set must be double-quoted in DDL.
RESERVED: frozenset[str] = frozenset(_loadReserved())

# Kept for compatibility — callers that import * from this module.
__all__ = ["RESERVED", "get_reserved"]


def get_reserved() -> frozenset[str]:
    """Return the reserved keywords frozenset (current API)."""
    return RESERVED

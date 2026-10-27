"""Per-engine canonical DDL for the ClickHouse deploy journal.

Mirrors ``infrastructure.deploy.canonical_ddl`` but renders to the CH
dialect (MergeTree / ReplacingMergeTree, ``DateTime64``, no PG-specific
``SERIAL`` / ``TIMESTAMPTZ`` / ``BOOLEAN``).

The ``{schema}`` placeholder is the **target database name** — the
journal lives inside each user DB, with a ``deploy_`` prefix on the
table names to keep the namespace visible. Same convention as the PG
journal's ``"__deploy"`` schema, but physically inlined in each DB
because CH has no schema concept.
"""

from __future__ import annotations

from functools import lru_cache

#: The 3 deploy journal tables (must match the keys in the PG version).
TABLES: tuple[str, ...] = (
    "deploy_schema_version",
    "deploy_script_history",
    "deploy_script_audit_log",
)


@lru_cache(maxsize=64)
def canonical_deploy_ddl(target_db: str) -> dict[str, str]:
    """Render the 3 deploy journal DDLs for the given target database.

    All DDLs include ``ON CLUSTER default`` so the journal is replicated
    to every shard (and so cluster desync at write time is absorbed).
    Each DDL is ``CREATE TABLE IF NOT EXISTS`` so calling repeatedly
    is idempotent.

    Returned strings are the bodies (no header) — callers that write
    files prepend the header via ``ensure_header``.
    """
    return {
        "deploy_schema_version": (
            f"CREATE TABLE IF NOT EXISTS {target_db}.deploy_schema_version "
            f"ON CLUSTER default (\n"
            f"    id          UInt64 DEFAULT 0,\n"
            f"    version     String,\n"
            f"    applied_at  DateTime64(9, 'UTC') DEFAULT now64(9),\n"
            f"    source      String\n"
            f") ENGINE = MergeTree ORDER BY tuple() "
            f"SETTINGS allow_nullable_key = 1"
        ),
        "deploy_script_history": (
            f"CREATE TABLE IF NOT EXISTS {target_db}.deploy_script_history "
            f"ON CLUSTER default (\n"
            f"    script_name     String,\n"
            f"    script_type     String,\n"
            f"    checksum        String,\n"
            f"    success         UInt8,\n"
            f"    executed_at     DateTime64(9, 'UTC') DEFAULT now64(9),\n"
            f"    error_message   String,\n"
            f"    duration_ms     Int32\n"
            f") ENGINE = ReplacingMergeTree(executed_at) "
            f"ORDER BY (script_name, script_type)"
        ),
        "deploy_script_audit_log": (
            f"CREATE TABLE IF NOT EXISTS {target_db}.deploy_script_audit_log "
            f"ON CLUSTER default (\n"
            f"    id              UInt64 DEFAULT 0,\n"
            f"    script_name     String,\n"
            f"    script_type     String,\n"
            f"    checksum        String,\n"
            f"    success         UInt8,\n"
            f"    error_message   String,\n"
            f"    duration_ms     Int32,\n"
            f"    executed_at     DateTime64(9, 'UTC') DEFAULT now64(9),\n"
            f"    deploy_version  String,\n"
            f"    deploy_source   String\n"
            f") ENGINE = MergeTree ORDER BY tuple() "
            f"SETTINGS allow_nullable_key = 1"
        ),
    }


def canonical_deploy_checksums(target_db: str) -> dict[str, str]:
    """SHA-256 of each canonical table's DDL — for change detection.

    Currently unused (PG variant drives canonical-mismatch detection),
    but provided for symmetry.
    """
    import hashlib

    return {
        name: hashlib.sha256(ddl.encode()).hexdigest()
        for name, ddl in canonical_deploy_ddl(target_db).items()
    }

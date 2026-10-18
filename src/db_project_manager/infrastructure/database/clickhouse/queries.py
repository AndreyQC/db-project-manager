"""ClickHouse RE SQL queries (system.tables, system.columns, system.functions).

The naming convention mirrors ``infrastructure.database.postgres.queries``:
each constant is a single SQL string used by ``CHDatabaseAdapter`` for
reverse engineering the structure of one or more databases.

Key differences from PG/GP:

* **No schemas in PG sense.** ClickHouse "database" = namespace. We treat
  each user-visible database as a "schema" in our RE output so the rest of
  the pipeline (compare, deploy, etc.) is dialect-agnostic.
* **Engines are not just storage.** ``MergeTree`` family, ``Log``, ``Memory``,
  ``MaterializedView``, ``View``, ``Dictionary``, ``Distributed``, ``S3``,
  ``ODBC``, etc. Each engine has its own set of parameters we need to
  capture for DDL roundtrip.
* **No constraints.** Primary key in CH is the engine's ``ORDER BY`` tuple;
  there are no FK / UNIQUE / CHECK constraints.
* **TTL & skip indexes** are engine settings, not table-level objects.
* **Functions live in two places:** ``system.functions`` (regular UDFs) and
  ``system.table_functions`` (table-valued functions like ``url``, ``s3``,
  ``mysql``, etc.).
"""

from __future__ import annotations

# --- databases (a.k.a. "schemas" in our RE output) -----------------------

#: User-visible databases. System/admin DBs are excluded.
#: Uses ``clusterAllReplicas`` so a query that lands on a shard without
#: the database (cluster desync) still returns the right answer via the
#: other replicas. Duplicates are removed with ``SELECT DISTINCT``.
LIST_DATABASES = """
SELECT DISTINCT name
FROM clusterAllReplicas('default', system.databases)
WHERE name NOT IN ('system', 'INFORMATION_SCHEMA', 'information_schema', 'default')
ORDER BY name
"""

#: Single-database overview (used as the top-level "schema" entry).
GET_DATABASE_OVERVIEW = """
SELECT name, engine, comment
FROM clusterAllReplicas('default', system.databases)
WHERE name = %(db)s
LIMIT 1
"""

# --- tables, views, materialized views ----------------------------------

#: One row per user table in the given database. Excludes View,
#: MaterializedView (handled in LIST_VIEWS) and Dictionary (handled in
#: LIST_DICTIONARIES). ``clusterAllReplicas`` aggregates across shards
#: (returns duplicates if the same table exists on multiple shards —
#: deduplicated by ``SELECT DISTINCT`` on the object name).
LIST_OBJECTS = """
SELECT DISTINCT
    name,
    engine,
    engine_full,
    total_rows,
    total_bytes,
    metadata_modification_time,
    create_table_query,
    partition_key,
    sorting_key,
    primary_key,
    sampling_key,
    storage_policy
FROM clusterAllReplicas('default', system.tables)
WHERE database = %(db)s
  AND engine NOT IN ('View', 'MaterializedView', 'Dictionary')
  AND name NOT LIKE '.inner%%'
ORDER BY name
"""

#: ClickHouse views + materialized views.
#: ``dependencies_database``/``dependencies_table`` are non-empty for
#: MaterializedView (lists the source table). ``has_own_data`` is 0 for
#: views (no storage) and 1 for MVs only when they have an inner table.
LIST_VIEWS = """
SELECT DISTINCT
    name,
    engine,
    engine_full,
    metadata_modification_time,
    create_table_query,
    as_select,
    dependencies_database,
    dependencies_table,
    has_own_data
FROM clusterAllReplicas('default', system.tables)
WHERE database = %(db)s
  AND engine IN ('View', 'MaterializedView')
ORDER BY name
"""

# --- columns ------------------------------------------------------------

#: Columns of a single object (table / view / MV / dict).
#: CH has no schemas, so all columns are under one namespace. Note:
#: CH 25.x does not expose ``ttl_expression`` in ``system.columns``; the
#: per-table TTL lives in ``system.tables.storage_policy`` / engine
#: definition. Use the table-level TTL instead (captured in ``LIST_OBJECTS``).
GET_COLUMNS = """
SELECT DISTINCT
    name,
    type,
    default_kind,
    default_expression,
    comment,
    is_in_primary_key,
    is_in_sorting_key,
    is_in_partition_key,
    is_in_sampling_key,
    compression_codec
FROM clusterAllReplicas('default', system.columns)
WHERE database = %(db)s
  AND table = %(table)s
ORDER BY position
"""

# --- dictionaries (CH-specific object class) ----------------------------

#: CH dictionaries live in system.dictionaries; ``type`` is the storage
#: layout (hashed/flat/cache/...), ``source`` is a free-form text describing
#: the data source (S3/Executable/HTTP/MySQL/etc.). The 25.x schema is
#: flat — no nested struct fields are exposed.
LIST_DICTIONARIES = """
SELECT DISTINCT
    name,
    type,
    status,
    origin,
    bytes_allocated,
    element_count,
    lifetime_min,
    lifetime_max,
    source
FROM clusterAllReplicas('default', system.dictionaries)
WHERE database = %(db)s
ORDER BY name
"""

#: Skip indexes (DataSkippingIndex) per table — CH 25.x schema.
#: One row per (db, table, name). Index types: minmax / set /
#: bloom_filter / tokenbf_v1 / ngrambf_v1 etc. ``expr`` is the indexed
#: column/expression; ``granularity`` is index grain.
LIST_SKIP_INDEXES = """
SELECT DISTINCT
    name,
    type,
    expr,
    granularity,
    data_compressed_bytes,
    data_uncompressed_bytes,
    marks_bytes
FROM clusterAllReplicas('default', system.data_skipping_indices)
WHERE database = %(db)s
  AND table = %(table)s
ORDER BY name
"""

#: Storage stats per table — parts / bytes / marks. Aggregated over
#: ``system.parts`` (one row per part). Used for "is this table actually
#: small/large" indicators in the RE output.
GET_STORAGE_STATS = """
SELECT
    countIf(active = 1)         AS active_parts,
    countIf(active = 0)         AS inactive_parts,
    sum(rows)                   AS parts_total_rows,
    sum(bytes_on_disk)          AS parts_total_bytes
FROM clusterAllReplicas('default', system.parts)
WHERE database = %(db)s
  AND table = %(table)s
"""

#: In-flight (not done) mutations on a database — for the deploy pre-flight
#: check ("don't deploy while an ALTER TABLE DELETE is running").
LIST_PENDING_MUTATIONS = """
SELECT DISTINCT
    table,
    mutation_id,
    command,
    parts_to_do,
    is_killed,
    latest_fail_reason
FROM clusterAllReplicas('default', system.mutations)
WHERE database = %(db)s
  AND NOT is_done
ORDER BY table, mutation_id
"""

# --- functions (UDFs; regular functions are too noisy) -------------------

#: User-defined functions only (sqlglot already provides builtin list).
LIST_USER_FUNCTIONS = """
SELECT name, origin, create_query
FROM system.functions
WHERE origin IN ('SQLUserDefined', 'ExecutableUserDefined', 'ClickHouseUserDefined')
ORDER BY name
"""

#: Table-valued functions (url, s3, mysql, jdbc, ...).
LIST_TABLE_FUNCTIONS = """
SELECT name
FROM system.table_functions
ORDER BY name
"""

# --- privileges & grants ------------------------------------------------

#: Show all grants for the current user (used by ``check_can_create_db``).
SHOW_CURRENT_GRANTS = "SHOW GRANTS"

# --- deploy journal (Phase 10) -----------------------------------------
#
# The CH deploy journal lives **per target database** (mirroring the PG
# design where ``__deploy`` is a schema inside each target DB). Since CH
# has no schemas, the journal tables live directly in the target DB
# with a ``deploy_`` prefix on the name to keep the namespace visible.
# Tables: ``deploy_schema_version`` (append-only), ``deploy_script_history``
# (one row per (script_name, script_type) via ReplacingMergeTree),
# ``deploy_script_audit_log`` (append-only).
#
# Naming convention: parameters use the PG names (``schema``, ``source``,
# ``script_name``...) so the call sites are uniform; ``schema`` here
# means the **target database** name.

#: Latest calver version applied to a target database (or None).
#: Reads via ``clusterAllReplicas`` so the result is correct regardless
#: of which shard the connection lands on. ``FINAL`` is not needed
#: because deploy_schema_version is plain MergeTree (append-only).
GET_SCHEMA_VERSION = """
SELECT version
FROM clusterAllReplicas('default', {schema}.deploy_schema_version)
ORDER BY applied_at DESC, id DESC
LIMIT 1
"""

#: Append a new version row for a target database.
INSERT_SCHEMA_VERSION = """
INSERT INTO {schema}.deploy_schema_version
    (version, source)
VALUES
    (%(version)s, %(source)s)
"""

#: State lookup for a single (script_name, script_type) row in a
#: target database. ``FINAL`` collapses ReplacingMergeTree duplicates
#: to the latest version. ``clusterAllReplicas`` ensures we see the
#: write regardless of which shard the connection lands on.
GET_SCRIPT_HISTORY = """
SELECT
    script_name, script_type, checksum, success,
    error_message, duration_ms, executed_at
FROM clusterAllReplicas('default', {schema}.deploy_script_history) FINAL
WHERE script_name = %(script_name)s
  AND script_type = %(script_type)s
LIMIT 1
"""

#: UPSERT: Insert a new versioned row into ReplacingMergeTree; on read
#: FINAL collapses to the latest. Replaces any existing row for the
#: same (script_name, script_type) tuple.
UPSERT_SCRIPT_HISTORY = """
INSERT INTO {schema}.deploy_script_history
    (script_name, script_type, checksum, success,
     executed_at, error_message, duration_ms)
VALUES
    (%(script_name)s, %(script_type)s, %(checksum)s,
     %(success)s, now64(9), %(error_message)s, %(duration_ms)s)
"""

#: Append an audit row (always, regardless of state UPSERT).
INSERT_SCRIPT_AUDIT = """
INSERT INTO {schema}.deploy_script_audit_log
    (script_name, script_type, checksum, success,
     error_message, duration_ms, deploy_version, deploy_source)
VALUES
    (%(script_name)s, %(script_type)s, %(checksum)s,
     %(success)s, %(error_message)s, %(duration_ms)s,
     %(deploy_version)s, %(deploy_source)s)
"""

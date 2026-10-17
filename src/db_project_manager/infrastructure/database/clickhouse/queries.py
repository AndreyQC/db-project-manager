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

#: One row per user table/view/MV/distributed/etc. in the given database.
#: ``clusterAllReplicas`` aggregates across shards (returns duplicates if
#: the same table exists on multiple shards — deduplicated by ``SELECT
#: DISTINCT`` on the object name).
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
  AND engine NOT IN ('View')
  AND name NOT LIKE '.inner%%'
ORDER BY name
"""

#: ClickHouse views + materialized views.
LIST_VIEWS = """
SELECT DISTINCT
    name,
    engine,
    engine_full,
    metadata_modification_time,
    create_table_query,
    as_select
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

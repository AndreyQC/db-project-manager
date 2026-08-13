"""SQL queries against the PostgreSQL / Greenplum catalog.

Texts are kept separate from the adapter logic for readability and testing.
Parameter binding uses SQLAlchemy ``text()`` bind parameters (:schema, :table_name).
"""

from __future__ import annotations

# --- privileges / server info (Phase 2: validation deploy) ---

GET_CREATEDB_CHECK = """
    SELECT rolcreatedb
      FROM pg_roles
     WHERE rolname = current_user
"""

GET_SERVER_TIMESTAMP_UTC = """
    SELECT to_char(now() AT TIME ZONE 'UTC', 'YYYYMMDD"T"HH24MISS')
"""

# --- schemas ---

GET_SCHEMAS = """
    SELECT
        n.nspname AS schema_name,
        pg_catalog.obj_description(n.oid, 'pg_namespace') AS comment
    FROM pg_catalog.pg_namespace n
    WHERE n.nspname NOT LIKE 'pg_%'
      AND n.nspname != 'information_schema'
    ORDER BY n.nspname;
"""

# --- tables ---

GET_TABLES = """
    SELECT
        t.table_name,
        pgd.description AS table_comment
    FROM information_schema.tables AS t
    LEFT JOIN pg_catalog.pg_namespace AS n ON n.nspname = t.table_schema
    LEFT JOIN pg_catalog.pg_class AS c
        ON c.relname = t.table_name AND c.relnamespace = n.oid
    LEFT JOIN pg_catalog.pg_description AS pgd
        ON pgd.objoid = c.oid AND pgd.objsubid = 0
    WHERE t.table_type = 'BASE TABLE'
      AND t.table_schema = :schema
    ORDER BY t.table_name
"""

# --- columns ---

GET_COLUMNS = """
    SELECT
        c.column_name,
        c.udt_name,
        c.is_nullable,
        c.column_default,
        c.character_maximum_length,
        c.numeric_precision,
        c.numeric_scale,
        pgd.description AS column_comment
    FROM information_schema.columns AS c
    LEFT JOIN pg_catalog.pg_namespace AS n ON n.nspname = c.table_schema
    LEFT JOIN pg_catalog.pg_class AS cl
        ON cl.relname = c.table_name AND cl.relnamespace = n.oid
    LEFT JOIN pg_catalog.pg_attribute AS a
        ON a.attrelid = cl.oid AND a.attname = c.column_name
    LEFT JOIN pg_catalog.pg_description AS pgd
        ON pgd.objoid = a.attrelid AND pgd.objsubid = a.attnum
    WHERE c.table_name = :table_name
      AND c.table_schema = :schema
    ORDER BY c.ordinal_position
"""

# --- constraints (PK / FK / UNIQUE / CHECK) ---

GET_CONSTRAINTS = """
    SELECT
        tc.constraint_name,
        tc.constraint_type,
        tc.table_schema AS schema,
        tc.table_name,
        kcu.column_name,
        ccu.table_schema AS referenced_table_schema,
        ccu.table_name AS referenced_table_name,
        ccu.column_name AS referenced_column_name,
        pg_get_constraintdef(c.oid) AS constraint_definition,
        obj_description(c.oid, 'pg_constraint') AS constraint_comment
    FROM information_schema.table_constraints AS tc
    LEFT JOIN information_schema.key_column_usage AS kcu
        ON tc.constraint_name = kcu.constraint_name
       AND tc.table_schema = kcu.table_schema
       AND tc.table_name = kcu.table_name
    LEFT JOIN information_schema.referential_constraints AS rc
        ON tc.constraint_name = rc.constraint_name
       AND tc.table_schema = rc.constraint_schema
    LEFT JOIN information_schema.constraint_column_usage AS ccu
        ON rc.unique_constraint_name = ccu.constraint_name
       AND rc.unique_constraint_schema = ccu.constraint_schema
    LEFT JOIN pg_catalog.pg_constraint AS c
        ON c.conname = tc.constraint_name
       AND c.connamespace = (SELECT oid FROM pg_namespace WHERE nspname = tc.table_schema)
    WHERE pg_get_constraintdef(c.oid) != ''
      AND tc.table_schema = :schema
      AND tc.table_name = :table_name
    ORDER BY
        CASE WHEN tc.constraint_type = 'FOREIGN KEY' THEN 1 ELSE 0 END,
        tc.constraint_name,
        kcu.ordinal_position
"""

# --- indexes ---

GET_INDEXES = """
    SELECT
        i.relname AS index_name,
        a.attname AS column_name,
        am.amname AS index_type,
        idx.indisunique AS is_unique,
        pg_get_expr(idx.indpred, idx.indrelid) AS filter_condition
    FROM pg_index AS idx
    JOIN pg_class AS i ON i.oid = idx.indexrelid
    JOIN pg_class AS t ON t.oid = idx.indrelid
    JOIN pg_namespace AS n ON n.oid = t.relnamespace
    JOIN pg_am AS am ON i.relam = am.oid
    JOIN pg_attribute AS a ON a.attrelid = t.oid AND a.attnum = ANY(idx.indkey)
    WHERE idx.indisprimary = false
      AND t.relkind = 'r'
      AND n.nspname = :schema
      AND t.relname = :table_name
    ORDER BY i.relname, array_position(idx.indkey, a.attnum)
"""

# --- sequences ---

# PostgreSQL 10+ via pg_sequence.
GET_SEQUENCES_POSTGRES = """
    SELECT
        seq.relname AS sequence_name,
        n.nspname AS schema_name,
        owning_tbl.relname AS owning_table,
        owning_col.attname AS owning_column,
        format_type(owning_col.atttypid, owning_col.atttypmod) AS data_type,
        s.seqstart AS start,
        s.seqincrement AS increment,
        s.seqmax AS maxvalue,
        s.seqmin AS minvalue,
        s.seqcache AS cache,
        CASE WHEN s.seqcycle THEN 'CYCLE' ELSE 'NO CYCLE' END AS cycle,
        pg_sequence_last_value(n.nspname || '.' || seq.relname) AS lastvalue,
        obj_description(seq.oid, 'pg_class') AS comment
    FROM pg_sequence s
    JOIN pg_class seq ON seq.oid = s.seqrelid
    JOIN pg_namespace n ON n.oid = seq.relnamespace
    LEFT JOIN pg_depend d ON d.objid = seq.oid AND d.deptype = 'a'
    LEFT JOIN pg_class owning_tbl ON owning_tbl.oid = d.refobjid
    LEFT JOIN pg_attribute owning_col
        ON owning_col.attrelid = d.refobjid AND owning_col.attnum = d.refobjsubid
    WHERE n.nspname NOT LIKE 'pg_%'
      AND n.nspname != 'information_schema'
      AND n.nspname = :schema
    ORDER BY n.nspname, seq.relname
"""

# Greenplum fallback (no pg_sequence): only partial info is available.
GET_SEQUENCES_GREENPLUM = """
    SELECT
        seq.relname AS sequence_name,
        n.nspname AS schema_name,
        owning_tbl.relname AS owning_table,
        owning_col.attname AS owning_column,
        format_type(owning_col.atttypid, owning_col.atttypmod) AS data_type,
        1 AS start,
        1 AS increment,
        NULL AS maxvalue,
        NULL AS minvalue,
        1 AS cache,
        'NO CYCLE' AS cycle,
        NULL AS lastvalue,
        obj_description(seq.oid, 'pg_class') AS comment
    FROM pg_class seq
    JOIN pg_namespace n ON n.oid = seq.relnamespace
    LEFT JOIN pg_depend d ON d.objid = seq.oid AND d.deptype = 'a'
    LEFT JOIN pg_class owning_tbl ON owning_tbl.oid = d.refobjid
    LEFT JOIN pg_attribute owning_col
        ON owning_col.attrelid = d.refobjid AND owning_col.attnum = d.refobjsubid
    WHERE seq.relkind = 'S'
      AND n.nspname NOT LIKE 'pg_%'
      AND n.nspname != 'information_schema'
      AND n.nspname = :schema
    ORDER BY n.nspname, seq.relname
"""

# --- views ---

GET_VIEWS = """
    SELECT
        c.relname AS view_name,
        n.nspname AS schema_name,
        obj_description(c.oid, 'pg_class') AS view_comment,
        pg_get_viewdef(c.oid, true) AS view_definition,
        a.attname AS column_name,
        col_description(a.attrelid, a.attnum) AS column_comment
    FROM pg_class AS c
    JOIN pg_namespace AS n ON n.oid = c.relnamespace
    JOIN pg_attribute AS a ON a.attrelid = c.oid
    WHERE c.relkind = 'v'
      AND n.nspname = :schema
      AND a.attnum > 0
      AND NOT a.attisdropped
      AND n.nspname NOT LIKE 'pg_%'
      AND n.nspname != 'information_schema'
    ORDER BY n.nspname, c.relname, a.attnum
"""

# --- materialized views ---

GET_MATERIALIZED_VIEWS = """
    SELECT
        c.relname AS view_name,
        n.nspname AS schema_name,
        COALESCE(t.spcname, 'pg_default') AS tablespace,
        CASE WHEN c.relispopulated THEN 'WITH DATA' ELSE 'WITH NO DATA' END AS data_status,
        obj_description(c.oid, 'pg_class') AS view_comment,
        pg_get_viewdef(c.oid) AS matview_definition,
        a.attname AS column_name,
        col_description(a.attrelid, a.attnum) AS column_comment
    FROM pg_class AS c
    JOIN pg_namespace AS n ON n.oid = c.relnamespace
    JOIN pg_attribute AS a
        ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
    LEFT JOIN pg_tablespace AS t ON t.oid = c.reltablespace
    LEFT JOIN pg_attrdef AS d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
    WHERE c.relkind = 'm'
      AND n.nspname = :schema
      AND n.nspname NOT LIKE 'pg_%'
      AND n.nspname != 'information_schema'
    ORDER BY n.nspname, c.relname, a.attnum
"""

# --- functions ---

GET_FUNCTIONS = """
    SELECT
        p.proname AS function_name,
        n.nspname AS schema_name,
        pg_get_function_result(p.oid) AS return_type,
        pg_get_function_arguments(p.oid) AS arguments,
        array_to_string(
            array(
                SELECT t.typname
                FROM unnest(p.proargtypes) AS argtype
                JOIN pg_type t ON t.oid = argtype
            ),
            ', '
        ) AS argument_types,
        l.lanname AS language,
        p.proretset AS returns_set,
        pg_get_functiondef(p.oid) AS function_definition,
        obj_description(p.oid, 'pg_proc') AS function_comment
    FROM pg_proc AS p
    LEFT JOIN pg_namespace AS n ON n.oid = p.pronamespace
    LEFT JOIN pg_language AS l ON l.oid = p.prolang
    WHERE n.nspname = :schema
      AND n.nspname NOT LIKE 'pg_%'
      AND n.nspname != 'information_schema'
      AND p.prokind = 'f'
      AND NOT EXISTS (
          SELECT 1 FROM pg_depend d
          WHERE d.objid = p.oid AND d.deptype = 'e'
      )
    ORDER BY n.nspname, p.proname
"""

# --- procedures ---

GET_PROCEDURES = """
    SELECT
        p.proname AS procedure_name,
        n.nspname AS schema_name,
        pg_get_function_arguments(p.oid) AS arguments,
        array_to_string(
            array(
                SELECT t.typname
                FROM unnest(p.proargtypes) AS argtype
                JOIN pg_type t ON t.oid = argtype
            ),
            ', '
        ) AS argument_types,
        l.lanname AS language,
        p.proretset AS returns_set,
        pg_get_functiondef(p.oid) AS procedure_definition,
        obj_description(p.oid, 'pg_proc') AS procedure_comment
    FROM pg_proc AS p
    LEFT JOIN pg_namespace AS n ON n.oid = p.pronamespace
    LEFT JOIN pg_language AS l ON l.oid = p.prolang
    WHERE n.nspname = :schema
      AND n.nspname NOT LIKE 'pg_%'
      AND n.nspname != 'information_schema'
      AND p.prokind = 'p'
      AND NOT EXISTS (
          SELECT 1 FROM pg_depend d
          WHERE d.objid = p.oid AND d.deptype = 'e'
      )
    ORDER BY n.nspname, p.proname
"""

# --- extensions (Phase 5) ---

GET_EXTENSIONS = """
    SELECT
        e.extname AS name,
        n.nspname AS schema,
        e.extversion AS version,
        obj_description(e.oid, 'pg_extension') AS comment
    FROM pg_extension e
    LEFT JOIN pg_namespace n ON n.oid = e.extnamespace
    ORDER BY e.extname
"""

# --- database properties / settings (Phase 5) ---

# Properties of the current database that affect DDL/DML behaviour.
# Only behaviour-relevant fields are extracted (datconnlimit / datistemplate /
# datallowconn are operational properties of the source server — NOT carried over).
GET_DATABASE_PROPERTIES = """
    SELECT
        pg_encoding_to_char(d.encoding) AS encoding,
        d.datcollate AS lc_collate,
        d.datctype AS lc_ctype
    FROM pg_database d
    WHERE d.datname = current_database()
"""

# Explicitly set database-level parameters only (setrole = 0 filters out
# role-specific settings — roles are out of scope for the tool).
# Each setconfig element is a "param=value" string; splitting is adapter-side.
GET_DATABASE_SETTINGS = """
    SELECT
        unnest(s.setconfig) AS setting
    FROM pg_db_role_setting s
    JOIN pg_database d ON d.oid = s.setdatabase
    WHERE d.datname = current_database()
      AND s.setrole = 0
    ORDER BY 1
"""

# --- table row counts (Phase 9: compare feature) ---
# reltuples is the planner estimate; cheap to read (no COUNT(*) scan), good
# enough as an informational "has data?" marker in the diff report.

GET_TABLE_ROW_COUNTS = """
    SELECT n.nspname AS schema_name,
           c.relname AS table_name,
           c.reltuples AS estimated_rows
    FROM pg_catalog.pg_class c
    JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relkind = 'r'
      AND n.nspname NOT IN ('pg_catalog', 'information_schema')
    ORDER BY c.reltuples DESC NULLS LAST
"""


# --- Phase 10: CD Foundation (__deploy schema) surface ---
#
# Tables live in a configurable schema (default __deploy); schema_name is
# interpolated as a *double-quoted identifier*. The caller (PGDatabaseAdapter)
# whitelists it via the same rule as _validate_db_name (LESSONS §19), so SQL
# injection through schema_name is impossible.
#
# All identifiers are fully-qualified and double-quoted (LESSONS §35).

# Latest applied version = the most recent row by applied_at (or id).
# Returns one row (version TEXT) or none when the table is empty.
GET_SCHEMA_VERSION = """
    SELECT version
    FROM {schema}.schema_version
    ORDER BY applied_at DESC, id DESC
    LIMIT 1
"""

# Append-only insert of a deploy version. source: 'validate' | 'deploy' | 'manual'.
INSERT_SCHEMA_VERSION = """
    INSERT INTO {schema}.schema_version (version, source)
    VALUES (:version, :source)
"""

# State lookup — PK (script_name, script_type) → at most one row.
# Returns None when missing (script never ran).
GET_SCRIPT_HISTORY = """
    SELECT script_name, script_type, checksum, success,
           error_message, duration_ms, executed_at
    FROM {schema}.script_history
    WHERE script_name = :script_name AND script_type = :script_type
"""

# State UPSERT: one row per (script_name, script_type). Updates the "currently
# applied" view on every execution (success or failure).
UPSERT_SCRIPT_HISTORY = """
    INSERT INTO {schema}.script_history
        (script_name, script_type, checksum, success, executed_at,
         error_message, duration_ms)
    VALUES
        (:script_name, :script_type, :checksum, :success, :executed_at,
         :error_message, :duration_ms)
    ON CONFLICT (script_name, script_type) DO UPDATE SET
        checksum     = EXCLUDED.checksum,
        success      = EXCLUDED.success,
        executed_at  = EXCLUDED.executed_at,
        error_message = EXCLUDED.error_message,
        duration_ms  = EXCLUDED.duration_ms
"""

# Append-only history: every attempt is recorded (never UPDATE'd). Carries
# deploy_version/deploy_source so reports (Phase 13 CD-17) can JOIN executions
# to a specific deploy.
INSERT_SCRIPT_AUDIT_LOG = """
    INSERT INTO {schema}.script_audit_log
        (script_name, script_type, checksum, success, error_message,
         duration_ms, executed_at, deploy_version, deploy_source)
    VALUES
        (:script_name, :script_type, :checksum, :success, :error_message,
         :duration_ms, :executed_at, :deploy_version, :deploy_source)
"""

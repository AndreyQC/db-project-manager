"""SQL queries against the PostgreSQL / Greenplum catalog.

Texts are kept separate from the adapter logic for readability and testing.
Parameter binding uses SQLAlchemy ``text()`` bind parameters (:schema, :table_name).
"""

from __future__ import annotations

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
        format_type(owning_col.atttypid, owning_col.atttypod) AS data_type,
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
        format_type(owning_col.atttypid, owning_col.atttypod) AS data_type,
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
    ORDER BY n.nspname, p.proname
"""

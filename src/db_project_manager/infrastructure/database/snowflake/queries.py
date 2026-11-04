"""SQL for the Snowflake adapter (Phase 21, task 20261103_002).

Object listing uses ``SHOW`` commands (they carry comments and, for views /
materialized views, the defining query text); typed fields come from
INFORMATION_SCHEMA (bind-parameter friendly). Object bodies come from
``GET_DDL``. Only whitelisted, regex-validated identifiers are interpolated
into SHOW/GET_DDL text (binds are impossible there) — same injection
discipline as the PG adapter.

S2 stage of plan 20261103_002; S1 carried only PING.
"""

#: Sanity check issued right after connect (exercises the wire and the
#: session context: warehouse/role/database resolve before real work).
PING = "SELECT 1"

#: Columns of one table, ordered. COMMENT is present on INFORMATION_SCHEMA.COLUMNS.
GET_COLUMNS = """
    SELECT
        COLUMN_NAME,
        DATA_TYPE,
        IS_NULLABLE,
        COLUMN_DEFAULT,
        CHARACTER_MAXIMUM_LENGTH,
        NUMERIC_PRECISION,
        NUMERIC_SCALE,
        COMMENT
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = :schema
      AND TABLE_NAME = :table
    ORDER BY ORDINAL_POSITION
"""

#: Constraints of one table with their columns and, for FKs, the referenced
#: (schema, table, column) resolved through UNIQUE_CONSTRAINT_NAME. CHECK
#: rows are filtered out downstream (Snowflake CHECK is hybrid-table-only).
GET_CONSTRAINTS = """
    SELECT
        tc.CONSTRAINT_NAME,
        tc.CONSTRAINT_TYPE,
        tc.TABLE_NAME,
        kcu.COLUMN_NAME,
        rc.UNIQUE_CONSTRAINT_NAME,
        ref_tc.TABLE_SCHEMA,
        ref_tc.TABLE_NAME,
        ref_kcu.COLUMN_NAME
    FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
    LEFT JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
        ON kcu.CONSTRAINT_SCHEMA = tc.CONSTRAINT_SCHEMA
        AND kcu.CONSTRAINT_NAME = tc.CONSTRAINT_NAME
    LEFT JOIN INFORMATION_SCHEMA.REFERENTIAL_CONSTRAINTS rc
        ON rc.CONSTRAINT_SCHEMA = tc.CONSTRAINT_SCHEMA
        AND rc.CONSTRAINT_NAME = tc.CONSTRAINT_NAME
    LEFT JOIN INFORMATION_SCHEMA.TABLE_CONSTRAINTS ref_tc
        ON ref_tc.CONSTRAINT_SCHEMA = rc.UNIQUE_CONSTRAINT_SCHEMA
        AND ref_tc.CONSTRAINT_NAME = rc.UNIQUE_CONSTRAINT_NAME
    LEFT JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE ref_kcu
        ON ref_kcu.CONSTRAINT_SCHEMA = rc.UNIQUE_CONSTRAINT_SCHEMA
        AND ref_kcu.CONSTRAINT_NAME = rc.UNIQUE_CONSTRAINT_NAME
    WHERE tc.TABLE_SCHEMA = :schema
      AND tc.TABLE_NAME = :table
    ORDER BY tc.CONSTRAINT_NAME, kcu.ORDINAL_POSITION
"""

#: Per-object DDL. ``kind`` must be one of GET_DDL_KINDS (adapter-validated);
#: ``fq`` = "DB.SCHEMA.NAME" (["(TYPE, ...)"] for routines) — adapter-validated.
GET_DDL_SQL = "SELECT GET_DDL(:kind, :fq)"

#: Whitelist for GET_DDL's first argument (adapter interpolates nothing else).
GET_DDL_KINDS = (
    "TABLE",
    "VIEW",
    "MATERIALIZED VIEW",
    "SEQUENCE",
    "FUNCTION",
    "PROCEDURE",
)

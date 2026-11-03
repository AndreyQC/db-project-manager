"""SQL for the Snowflake adapter (Phase 21, task 20261103_002).

S1 carries only the sanity query. Stage S2 adds the metadata queries
(INFORMATION_SCHEMA object lists + ``GET_DDL`` per object) — see plan
``harness_docs/harness_tasks/2026-11-03/20261103_002_snowflake_connect_re_plan.md``.
"""

#: Sanity check issued right after connect (exercises the wire and the
#: session context: warehouse/role/database resolve before real work).
PING = "SELECT 1"

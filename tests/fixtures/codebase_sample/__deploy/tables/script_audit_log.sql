/*====================================================================================
[<[autodoc-yaml]]
object:
  object_catalog: demo
  object_schema: __deploy
  object_type: table
  object_name: script_audit_log
  object_key: pg_database/demo/schema/__deploy/type/table/name/script_audit_log
project:
  build: true
  immutable: true
[[autodoc-yaml]>]
=====================================================================================*/

CREATE TABLE IF NOT EXISTS "__deploy"."script_audit_log" (
    id              SERIAL PRIMARY KEY,
    script_name     TEXT NOT NULL,
    script_type     TEXT NOT NULL,
    checksum        TEXT NOT NULL,
    success         BOOLEAN NOT NULL,
    error_message   TEXT,
    duration_ms     INTEGER NOT NULL,
    executed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    deploy_version  TEXT,
    deploy_source   TEXT
);

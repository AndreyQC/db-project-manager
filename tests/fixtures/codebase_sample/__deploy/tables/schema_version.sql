/*====================================================================================
[<[autodoc-yaml]]
object:
  object_catalog: demo
  object_schema: __deploy
  object_type: table
  object_name: schema_version
  object_key: pg_database/demo/schema/__deploy/type/table/name/schema_version
project:
  build: true
  immutable: true
[[autodoc-yaml]>]
=====================================================================================*/

CREATE TABLE IF NOT EXISTS "__deploy"."schema_version" (
    id              SERIAL PRIMARY KEY,
    version         TEXT NOT NULL,
    applied_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    source          TEXT NOT NULL
);

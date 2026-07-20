/*====================================================================================
[<[autodoc-yaml]]
object:
  object_catalog: demo
  object_schema: null
  object_type: extension
  object_name: citext
  object_key: pg_database/demo/type/extension/name/citext
  extension_version: '1.6'
project:
  build: true
[[autodoc-yaml]>]
=====================================================================================*/

-- Создание расширения "citext"
CREATE EXTENSION IF NOT EXISTS "citext"
SCHEMA "public";

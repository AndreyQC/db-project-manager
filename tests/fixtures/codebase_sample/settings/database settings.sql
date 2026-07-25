/*====================================================================================
[<[autodoc-yaml]]
object:
  object_catalog: demo
  object_schema: null
  object_type: database_setting
  object_name: database settings
  object_key: pg_database/demo/type/database_setting/name/database settings
  properties:
    encoding: UTF8
    lc_collate: C
    lc_ctype: C
    template: template0
project:
  build: true
[[autodoc-yaml]>]
=====================================================================================*/

-- Параметры уровня базы (ALTER DATABASE ... SET).
ALTER DATABASE "demo" SET work_mem = '64MB';
ALTER DATABASE "demo" SET search_path = '$user, public';

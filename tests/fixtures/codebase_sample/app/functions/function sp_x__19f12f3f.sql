/*====================================================================================
[<[autodoc-yaml]]
object:
  object_catalog: demo
  object_schema: app
  object_type: function
  object_name: sp_x
  object_key: pg_database/demo/schema/app/type/function/name/sp_x/signature/19f12f3f
  object_signature: '19f12f3f'
project:
  build: true
[[autodoc-yaml]>]
=====================================================================================*/

-- Создание функции app.sp_x(int4)
CREATE OR REPLACE FUNCTION app.sp_x(a int4) RETURNS int4 LANGUAGE sql AS $$ SELECT a $$;

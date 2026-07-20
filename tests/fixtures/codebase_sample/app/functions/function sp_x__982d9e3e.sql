/*====================================================================================
[<[autodoc-yaml]]
object:
  object_catalog: demo
  object_schema: app
  object_type: function
  object_name: sp_x
  object_key: pg_database/demo/schema/app/type/function/name/sp_x/signature/982d9e3e
  object_signature: '982d9e3e'
project:
  build: true
[[autodoc-yaml]>]
=====================================================================================*/

-- Создание функции app.sp_x(text)
CREATE OR REPLACE FUNCTION app.sp_x(a text) RETURNS text LANGUAGE sql AS $$ SELECT a $$;

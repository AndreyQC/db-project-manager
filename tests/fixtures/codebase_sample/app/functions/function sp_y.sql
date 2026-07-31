/*====================================================================================
[<[autodoc-yaml]]
object:
  object_catalog: demo
  object_schema: app
  object_type: function
  object_name: sp_y
  object_key: pg_database/demo/schema/app/type/function/name/sp_y/signature/75666699
  object_signature: '75666699'
  argument_types: uuid
project:
  build: true
[[autodoc-yaml]>]
=====================================================================================*/

-- Создание функции app.sp_y(uuid)
CREATE OR REPLACE FUNCTION app.sp_y(id uuid) RETURNS boolean LANGUAGE sql AS $$ SELECT true $$;

/*====================================================================================
[<[autodoc-yaml]]
object:
  object_catalog: demo
  object_schema: app
  object_type: function
  object_name: sp_caller
  object_key: pg_database/demo/schema/app/type/function/name/sp_caller/signature/75666699
  object_signature: '75666699'
project:
  build: true
[[autodoc-yaml]>]
=====================================================================================*/

-- Создание функции app.sp_caller(uuid)
-- Regression for Phase 6 follow-up: a function calling another function with a
-- schema-qualified name inside CASE WHEN (or any non-FROM context) must produce
-- a DEPENDS_ON edge (previously missed because _classify_at only handled
-- FK/nextval/JOIN/DML/SELECT).
CREATE OR REPLACE FUNCTION app.sp_caller(p_id uuid) RETURNS boolean LANGUAGE sql AS $$
    SELECT CASE WHEN app.sp_y(p_id) IS NULL THEN FALSE ELSE TRUE END;
$$;

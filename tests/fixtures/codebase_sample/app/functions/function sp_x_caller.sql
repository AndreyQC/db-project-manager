/*====================================================================================
[<[autodoc-yaml]]
object:
  object_catalog: demo
  object_schema: app
  object_type: function
  object_name: sp_x_caller
  object_key: pg_database/demo/schema/app/type/function/name/sp_x_caller/signature/75666699
  object_signature: '75666699'
  argument_types: int4
project:
  build: true
[[autodoc-yaml]>]
=====================================================================================*/

-- Phase 8: calls both overloads of sp_x with literal arguments so the edge
-- scanner must route one DEPENDS_ON edge to the int4 overload and another to
-- the text overload (instead of "first wins" for both).
CREATE OR REPLACE FUNCTION app.sp_x_caller(flag int4) RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    PERFORM app.sp_x(123);       -- integer literal -> int4 overload (19f12f3f)
    PERFORM app.sp_x('hello');   -- string literal  -> text overload (982d9e3e)
END;
$$;

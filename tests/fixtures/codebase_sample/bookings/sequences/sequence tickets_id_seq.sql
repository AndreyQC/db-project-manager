/*====================================================================================
[<[autodoc-yaml]]
object:
  object_catalog: demo
  object_schema: bookings
  object_type: sequence
  object_name: tickets_id_seq
  object_key: pg_database/demo/schema/bookings/type/sequence/name/tickets_id_seq
project:
  build: true
[[autodoc-yaml]>]
=====================================================================================*/

-- Создание последовательности bookings.tickets_id_seq
CREATE SEQUENCE bookings.tickets_id_seq
    INCREMENT BY 1
    START 1
    CACHE 1;

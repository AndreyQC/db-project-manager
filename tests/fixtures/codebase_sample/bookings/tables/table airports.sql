/*====================================================================================
[<[autodoc-yaml]]
object:
  object_catalog: demo
  object_schema: bookings
  object_type: table
  object_name: airports
  object_key: pg_database/demo/schema/bookings/type/table/name/airports
project:
  build: true
[[autodoc-yaml]>]
=====================================================================================*/

-- Создание таблицы bookings.airports
CREATE TABLE bookings.airports (
    airport_code bpchar(3) NOT NULL,
    airport_name text NOT NULL,
    city text NOT NULL,
    CONSTRAINT airports_pkey PRIMARY KEY (airport_code)
);

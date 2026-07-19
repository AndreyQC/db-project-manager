/*====================================================================================
[<[autodoc-yaml]]
object:
  object_catalog: demo
  object_schema: bookings
  object_type: table
  object_name: aircrafts
  object_key: pg_database/demo/schema/bookings/type/table/name/aircrafts
project:
  build: true
[[autodoc-yaml]>]
=====================================================================================*/

-- Создание таблицы bookings.aircrafts
CREATE TABLE bookings.aircrafts (
    aircraft_code bpchar(3) NOT NULL,
    model text NOT NULL,
    range int4 NOT NULL,
    CONSTRAINT aircrafts_pkey PRIMARY KEY (aircraft_code),
    CONSTRAINT aircrafts_range_check CHECK ((range > 0))
);

/*====================================================================================
[<[autodoc-yaml]]
object:
  object_catalog: demo
  object_schema: bookings
  object_type: index
  object_name: flights_flight_no_idx
  object_key: pg_database/demo/schema/bookings/type/index/name/flights_flight_no_idx
project:
  build: true
[[autodoc-yaml]>]
=====================================================================================*/

-- Автономный индекс таблицы bookings.flights
CREATE INDEX flights_flight_no_idx ON bookings.flights USING btree (flight_no);

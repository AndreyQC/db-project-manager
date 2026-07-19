/*====================================================================================
[<[autodoc-yaml]]
object:
  object_catalog: demo
  object_schema: bookings
  object_type: view
  object_name: flights_v
  object_key: pg_database/demo/schema/bookings/type/view/name/flights_v
project:
  build: true
[[autodoc-yaml]>]
=====================================================================================*/

-- Создание представления bookings.flights_v
CREATE OR REPLACE VIEW bookings.flights_v AS
    SELECT f.flight_id,
           f.flight_no,
           a.model AS aircraft_model
      FROM bookings.flights AS f
      LEFT JOIN bookings.aircrafts AS a ON f.aircraft_code = a.aircraft_code;

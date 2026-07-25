/*====================================================================================
[<[autodoc-yaml]]
object:
  object_catalog: demo
  object_schema: bookings
  object_type: materialized_view
  object_name: routes
  object_key: pg_database/demo/schema/bookings/type/materialized_view/name/routes
project:
  build: false
[[autodoc-yaml]>]
=====================================================================================*/

-- Создание материализованного представления bookings.routes
-- (build: false — отключено от деплоя, но остаётся в графе зависимостей)
CREATE MATERIALIZED VIEW bookings.routes AS
    SELECT f.departure_airport,
           f.arrival_airport,
           count(*) AS flights_count
      FROM bookings.flights AS f
     GROUP BY f.departure_airport, f.arrival_airport;

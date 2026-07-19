/*====================================================================================
[<[autodoc-yaml]]
object:
  object_catalog: demo
  object_schema: bookings
  object_type: table
  object_name: flights
  object_key: pg_database/demo/schema/bookings/type/table/name/flights
project:
  build: true
[[autodoc-yaml]>]
=====================================================================================*/

-- Создание таблицы bookings.flights
CREATE TABLE bookings.flights (
    flight_id int4 NOT NULL,
    flight_no char(6) NOT NULL,
    departure_airport bpchar(3) NOT NULL,
    arrival_airport bpchar(3) NOT NULL,
    aircraft_code bpchar(3) NOT NULL,
    CONSTRAINT flights_pkey PRIMARY KEY (flight_id),
    CONSTRAINT flights_aircraft_code_fkey FOREIGN KEY (aircraft_code)
        REFERENCES bookings.aircrafts(aircraft_code),
    CONSTRAINT flights_departure_airport_fkey FOREIGN KEY (departure_airport)
        REFERENCES bookings.airports(airport_code),
    CONSTRAINT flights_arrival_airport_fkey FOREIGN KEY (arrival_airport)
        REFERENCES bookings.airports(airport_code)
);

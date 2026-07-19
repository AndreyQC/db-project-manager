/*====================================================================================
[<[autodoc-yaml]]
object:
  object_catalog: demo
  object_schema: bookings
  object_type: table
  object_name: tickets
  object_key: pg_database/demo/schema/bookings/type/table/name/tickets
project:
  build: true
[[autodoc-yaml]>]
=====================================================================================*/

-- Создание таблицы bookings.tickets
CREATE TABLE bookings.tickets (
    ticket_id int4 NOT NULL DEFAULT nextval('bookings.tickets_id_seq'),
    flight_id int4 NOT NULL,
    passenger_name text NOT NULL,
    CONSTRAINT tickets_pkey PRIMARY KEY (ticket_id),
    CONSTRAINT tickets_flight_id_fkey FOREIGN KEY (flight_id)
        REFERENCES bookings.flights(flight_id)
);

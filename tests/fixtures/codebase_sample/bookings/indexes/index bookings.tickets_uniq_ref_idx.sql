-- Unique index without autodoc header (token-path parsing test).
CREATE UNIQUE INDEX bookings.tickets_uniq_ref_idx ON bookings.tickets (flight_id, passenger_name);

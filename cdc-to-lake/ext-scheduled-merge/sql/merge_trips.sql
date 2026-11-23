-- Merge the run's trips ({source}: trips_source.sql) into {trips}. Only the first WHEN clause that matches runs
-- (MERGE INTO), so the order of the clauses is the rule:
--   1. the source's furthest event is further than the row's (rank, then event_time, then event_id): take its
--      state; keep the row's driver and fare where the source has none; take the source's request when it is newer
--   2. otherwise, fill only what the row lacks, and take a newer request (a corrected request changes the zone
--      and the request time, never the state)
--   3. a trip the table does not have yet is inserted
-- "The request is newer": the source has a request and the row has none, or it is later, or equally late with a
-- larger event_id. Merging the same events again matches no clause, so the table does not change.
MERGE INTO {trips} t
USING ({source}) s
ON t.trip_id = s.trip_id
WHEN MATCHED AND (s.last_event_rank > t.last_event_rank
     OR (s.last_event_rank = t.last_event_rank AND (s.last_event_time > t.last_event_time
         OR (s.last_event_time = t.last_event_time AND s.last_event_id > t.last_event_id))))
  THEN UPDATE SET
    trip_state = s.trip_state,
    last_event_time = s.last_event_time,
    last_event_rank = s.last_event_rank,
    last_event_id = s.last_event_id,
    driver_id = COALESCE(s.driver_id, t.driver_id),
    fare = COALESCE(s.fare, t.fare),
    pickup_zone_id = CASE WHEN s.request_event_id IS NOT NULL AND (t.request_event_id IS NULL
        OR s.requested_time > t.requested_time
        OR (s.requested_time = t.requested_time AND s.request_event_id > t.request_event_id))
      THEN s.pickup_zone_id ELSE t.pickup_zone_id END,
    requested_time = CASE WHEN s.request_event_id IS NOT NULL AND (t.request_event_id IS NULL
        OR s.requested_time > t.requested_time
        OR (s.requested_time = t.requested_time AND s.request_event_id > t.request_event_id))
      THEN s.requested_time ELSE t.requested_time END,
    request_event_id = CASE WHEN s.request_event_id IS NOT NULL AND (t.request_event_id IS NULL
        OR s.requested_time > t.requested_time
        OR (s.requested_time = t.requested_time AND s.request_event_id > t.request_event_id))
      THEN s.request_event_id ELSE t.request_event_id END
WHEN MATCHED AND ((s.request_event_id IS NOT NULL AND (t.request_event_id IS NULL
        OR s.requested_time > t.requested_time
        OR (s.requested_time = t.requested_time AND s.request_event_id > t.request_event_id)))
     OR (t.driver_id IS NULL AND s.driver_id IS NOT NULL)
     OR (t.fare IS NULL AND s.fare IS NOT NULL))
  THEN UPDATE SET
    driver_id = COALESCE(t.driver_id, s.driver_id),
    fare = COALESCE(t.fare, s.fare),
    pickup_zone_id = CASE WHEN s.request_event_id IS NOT NULL AND (t.request_event_id IS NULL
        OR s.requested_time > t.requested_time
        OR (s.requested_time = t.requested_time AND s.request_event_id > t.request_event_id))
      THEN s.pickup_zone_id ELSE t.pickup_zone_id END,
    requested_time = CASE WHEN s.request_event_id IS NOT NULL AND (t.request_event_id IS NULL
        OR s.requested_time > t.requested_time
        OR (s.requested_time = t.requested_time AND s.request_event_id > t.request_event_id))
      THEN s.requested_time ELSE t.requested_time END,
    request_event_id = CASE WHEN s.request_event_id IS NOT NULL AND (t.request_event_id IS NULL
        OR s.requested_time > t.requested_time
        OR (s.requested_time = t.requested_time AND s.request_event_id > t.request_event_id))
      THEN s.request_event_id ELSE t.request_event_id END
WHEN NOT MATCHED
  THEN INSERT (trip_id, trip_state, pickup_zone_id, requested_time, request_event_id, driver_id, fare,
               last_event_time, last_event_rank, last_event_id)
       VALUES (s.trip_id, s.trip_state, s.pickup_zone_id, s.requested_time, s.request_event_id, s.driver_id, s.fare,
               s.last_event_time, s.last_event_rank, s.last_event_id)

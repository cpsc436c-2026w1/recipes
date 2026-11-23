-- One source row per trip from the run's events ({events}): Athena raises an error when one target row matches more
-- than one source row (MERGE INTO), so the kinds are folded into one row here. The same text runs in Athena and in
-- DuckDB; only {events} differs (Athena: trip_events filtered by dt and "$path"; DuckDB: the run's lines).
--   state and last_event_*  from the furthest event: rank 1 requested, 2 accepted, 3 completed; within a rank the
--                           latest event_time, then the larger event_id
--   zone, requested_time,   from the newest request (latest event_time, then larger event_id)
--   request_event_id
--   driver_id               from the newest accept or completion
--   fare                    from the newest completion (a correction has a new event_id and a later event_time)
-- A repeated delivery carries the same event_id, so each event_id is kept once.
-- {events} is named once and every choice is a window over the same rows, then one GROUP BY: Athena does not keep
-- the result of a WITH query, so each reference to it would read the run's objects again (measured: four
-- references read them four times).
WITH once AS (
  SELECT event_type, event_id, trip_id, pickup_zone_id, driver_id, fare, event_time,
         CASE event_type WHEN 'trip_requested' THEN 1 WHEN 'trip_accepted' THEN 2 ELSE 3 END AS event_rank,
         ROW_NUMBER() OVER (PARTITION BY event_id ORDER BY event_time) AS copy_no
  FROM {events}
),
ranked AS (
  SELECT trip_id, event_rank, event_time, event_id, pickup_zone_id, driver_id, fare,
         ROW_NUMBER() OVER (PARTITION BY trip_id ORDER BY event_rank DESC, event_time DESC, event_id DESC) AS n,
         ROW_NUMBER() OVER (PARTITION BY trip_id, event_rank = 1 ORDER BY event_time DESC, event_id DESC) AS n_request,
         ROW_NUMBER() OVER (PARTITION BY trip_id, event_rank >= 2 ORDER BY event_time DESC, event_id DESC) AS n_driver,
         ROW_NUMBER() OVER (PARTITION BY trip_id, event_rank = 3 ORDER BY event_time DESC, event_id DESC) AS n_completion
  FROM once
  WHERE copy_no = 1
)
SELECT trip_id,
       CASE max(CASE WHEN n = 1 THEN event_rank END) WHEN 1 THEN 'requested' WHEN 2 THEN 'accepted'
            ELSE 'completed' END AS trip_state,
       max(CASE WHEN event_rank = 1 AND n_request = 1 THEN pickup_zone_id END) AS pickup_zone_id,
       max(CASE WHEN event_rank = 1 AND n_request = 1 THEN event_time END) AS requested_time,
       max(CASE WHEN event_rank = 1 AND n_request = 1 THEN event_id END) AS request_event_id,
       max(CASE WHEN event_rank >= 2 AND n_driver = 1 THEN driver_id END) AS driver_id,
       max(CASE WHEN event_rank = 3 AND n_completion = 1 THEN fare END) AS fare,
       max(CASE WHEN n = 1 THEN event_time END) AS last_event_time,
       max(CASE WHEN n = 1 THEN event_rank END) AS last_event_rank,
       max(CASE WHEN n = 1 THEN event_id END) AS last_event_id
FROM ranked
GROUP BY trip_id

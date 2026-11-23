-- Rebuild {zone_counts} for every {period} from {start_day} (inclusive) to {end_day} (exclusive), in one statement
-- (one Iceberg commit: a reader never sees a period half rebuilt). The counts are recomputed from {trips}; a group
-- that has a row now but no trips any more (a corrected request moved its trip) is joined in with 0 and deleted.
-- A trip without a request yet (its request is late) has no zone and is not counted until the request is merged.
MERGE INTO {zone_counts} t
USING (
  WITH fresh AS (
    SELECT CAST(date_trunc('{period}', requested_time) AS date) AS period_start,
           pickup_zone_id,
           count(*) AS trips_requested,
           sum(CASE WHEN trip_state = 'completed' THEN 1 ELSE 0 END) AS trips_completed,
           round(COALESCE(sum(fare), 0.0), 2) AS fare_total
    FROM {trips}
    WHERE requested_time >= TIMESTAMP '{start_day} 00:00:00'
      AND requested_time < TIMESTAMP '{end_day} 00:00:00'
      AND pickup_zone_id IS NOT NULL
    GROUP BY 1, 2
  ),
  kept AS (
    SELECT period_start, pickup_zone_id
    FROM {zone_counts}
    WHERE period_start >= DATE '{start_day}' AND period_start < DATE '{end_day}'
  )
  SELECT COALESCE(f.period_start, k.period_start) AS period_start,
         COALESCE(f.pickup_zone_id, k.pickup_zone_id) AS pickup_zone_id,
         COALESCE(f.trips_requested, 0) AS trips_requested,
         COALESCE(f.trips_completed, 0) AS trips_completed,
         COALESCE(f.fare_total, 0.0) AS fare_total
  FROM fresh f
  FULL OUTER JOIN kept k ON f.period_start = k.period_start AND f.pickup_zone_id = k.pickup_zone_id
) s
ON t.period_start = s.period_start AND t.pickup_zone_id = s.pickup_zone_id
WHEN MATCHED AND s.trips_requested = 0
  THEN DELETE
WHEN MATCHED AND (t.trips_requested <> s.trips_requested OR t.trips_completed <> s.trips_completed
     OR t.fare_total <> s.fare_total)
  THEN UPDATE SET trips_requested = s.trips_requested, trips_completed = s.trips_completed, fare_total = s.fare_total
WHEN NOT MATCHED AND s.trips_requested > 0
  THEN INSERT (period_start, pickup_zone_id, trips_requested, trips_completed, fare_total)
       VALUES (s.period_start, s.pickup_zone_id, s.trips_requested, s.trips_completed, s.fare_total)

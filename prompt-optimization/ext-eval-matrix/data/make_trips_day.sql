-- Builds data/trips.parquet: SIM01, the whole day of DS01 that the rides questions are about.
-- DS01: TLC High Volume For-Hire Vehicle trips, Uber (HV0003), March 2024. SIM01: the trips requested in pickup zones
-- 48, 161 and 230 on 2024-03-14 (New York local time, as in the source), in the course's `trips` columns.
-- Expected: 19,074 rows, 4,345 / 7,661 / 7,068 in zones 48 / 161 / 230 (measured with DuckDB over DS01, 2026-11-21).
-- Reads the whole month once (about 500 MB over HTTPS). From prompt-optimization/:
--   uvx --from duckdb-cli duckdb < ext-eval-matrix/data/make_trips_day.sql
-- then run the matrix with --trips ext-eval-matrix/data/trips.parquet. The reference values are queries, so they are
-- computed on the day. trip_id: DS01 has no trip id, so it is a hash of the row's columns and its position.
COPY (
  WITH day AS (
    SELECT *, row_number() OVER (ORDER BY request_datetime, PULocationID, dropoff_datetime, trip_miles) AS pos
    FROM 'https://d37ci6vzurychx.cloudfront.net/trip-data/fhvhv_tripdata_2024-03.parquet'
    WHERE hvfhs_license_num = 'HV0003'
      AND PULocationID IN (48, 161, 230)
      AND request_datetime >= TIMESTAMP '2024-03-14 00:00:00'
      AND request_datetime <  TIMESTAMP '2024-03-15 00:00:00'
  )
  SELECT substr(sha256(concat_ws('|', request_datetime, pickup_datetime, dropoff_datetime, PULocationID,
                                 DOLocationID, trip_miles, trip_time, base_passenger_fare, driver_pay, pos)), 1, 16)
           AS trip_id,
         'completed' AS trip_state,
         CAST(PULocationID AS INTEGER) AS pickup_zone_id,
         request_datetime AS requested_time,
         CAST(NULL AS VARCHAR) AS driver_id,
         base_passenger_fare AS fare,
         trip_miles
  FROM day ORDER BY pos
) TO 'ext-eval-matrix/data/trips.parquet' (FORMAT parquet);

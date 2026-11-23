-- SIM01 "march-sample": 1,000 of DS01's trips (TLC High Volume FHV, March 2024, Uber HV0003), requested in zones
-- 48, 161 and 230 from 2024-03-01 to 2024-03-30. 34 rows on days 2, 5, 8 ... 29 (2024-03-14 among them) and 33
-- on the others; inside a day the rows are taken in the order of an MD5 of the row, so the draw is the same each time.
-- Run with DuckDB beside the monthly file:  duckdb -c ".read ext-backfill/sim/extract_sim01.sql"
COPY (
  WITH src AS (
    SELECT * EXCLUDE (PULocationID, DOLocationID), PULocationID AS pulocationid, DOLocationID AS dolocationid
    FROM 'fhvhv_tripdata_2024-03.parquet'
    WHERE hvfhs_license_num = 'HV0003' AND PULocationID IN (48, 161, 230)
      AND request_datetime >= TIMESTAMP '2024-03-01' AND request_datetime < TIMESTAMP '2024-03-31'
  ), ranked AS (
    SELECT *, row_number() OVER (PARTITION BY CAST(request_datetime AS DATE)
                                 ORDER BY md5(CAST(src AS VARCHAR))) AS n
    FROM src
  )
  SELECT hvfhs_license_num, dispatching_base_num, originating_base_num, request_datetime, on_scene_datetime,
         pickup_datetime, dropoff_datetime, pulocationid, dolocationid, trip_miles, trip_time, base_passenger_fare,
         tolls, bcf, sales_tax, congestion_surcharge, airport_fee, tips, driver_pay, shared_request_flag,
         shared_match_flag, access_a_ride_flag, wav_request_flag, wav_match_flag
  FROM ranked
  WHERE n <= CASE WHEN day(request_datetime) % 3 = 2 THEN 34 ELSE 33 END
  ORDER BY request_datetime, pulocationid, n
) TO 'ext-backfill/sim/SIM01-march-sample.csv' (HEADER);

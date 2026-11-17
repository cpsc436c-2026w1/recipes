-- Latest state of each trip, rebuilt from the change log. Runs in Athena and in DuckDB unchanged.
-- Order by change_time, then seq: change_time has whole seconds only, seq orders changes within one second.
-- A trip whose latest change is REMOVE is gone from the table, so it is left out here.
CREATE OR REPLACE VIEW {view} AS
SELECT trip_id, status, pickup_zone, fare_quote, driver_id, change_time
FROM (
  SELECT *, row_number() OVER (PARTITION BY trip_id ORDER BY change_time DESC, seq DESC) AS rn
  FROM {table}
) latest
WHERE rn = 1 AND op <> 'REMOVE'

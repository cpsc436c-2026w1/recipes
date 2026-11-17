-- The change log as an Iceberg table in the Glue Data Catalog (Athena DDL; main/infra.py runs it).
-- One row per change to a DynamoDB item; columns match main/handler.py ITEM_FIELDS (tests/test_schema.py checks).
-- Partitioned by the day of the change (a hidden partition: queries filter on change_time, not on a day column),
-- so a query over the last day reads only that day's files.
CREATE TABLE IF NOT EXISTS {database}.{table} (
  trip_id      string,
  op           string,
  change_time  timestamp,
  arrival_time timestamp,
  seq          string,
  status       string,
  pickup_zone  string,
  fare_quote   double,
  driver_id    string
)
PARTITIONED BY (day(change_time))
LOCATION '{location}'
TBLPROPERTIES (
  'table_type' = 'ICEBERG',
  'format' = 'parquet',
  'write_compression' = 'zstd',
  'vacuum_max_snapshot_age_seconds' = '604800',
  'vacuum_min_snapshots_to_keep' = '5'
)

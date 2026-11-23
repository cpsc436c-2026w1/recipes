-- The three tables in Athena (scripts run by merge.py up, one statement each, split on lines of ";").
-- trip_events reads the raw JSON Lines objects; its day folders are found by partition projection, so no partition
-- has to be registered (Athena docs, partition projection, date type). trips and zone_counts are Iceberg tables.
CREATE DATABASE IF NOT EXISTS {database}
;
CREATE EXTERNAL TABLE IF NOT EXISTS {database}.trip_events (
  delivery_id     string,
  received_time   string,
  event_type      string,
  event_id        string,
  trip_id         string,
  pickup_zone_id  int,
  driver_id       string,
  dropoff_zone_id int,
  trip_miles      double,
  trip_time_s     int,
  fare            double,
  driver_pay      double,
  event_time      string
)
PARTITIONED BY (dt string)
ROW FORMAT SERDE 'org.openx.data.jsonserde.JsonSerDe'
LOCATION 's3://{bucket}/raw/trip-events/'
TBLPROPERTIES (
  'projection.enabled' = 'true',
  'projection.dt.type' = 'date',
  'projection.dt.format' = 'yyyy-MM-dd',
  'projection.dt.range' = '2026-01-01,NOW',
  'projection.dt.interval' = '1',
  'projection.dt.interval.unit' = 'DAYS',
  'storage.location.template' = 's3://{bucket}/raw/trip-events/dt=${{dt}}/'
)
;
CREATE TABLE IF NOT EXISTS {database}.trips (
  trip_id          string,
  trip_state       string,
  pickup_zone_id   int,
  requested_time   timestamp,
  request_event_id string,
  driver_id        string,
  fare             double,
  last_event_time  timestamp,
  last_event_rank  int,
  last_event_id    string
)
PARTITIONED BY (day(requested_time))
LOCATION 's3://{bucket}/lake/trips/'
TBLPROPERTIES ('table_type' = 'ICEBERG', 'format' = 'parquet', 'write_compression' = 'zstd')
;
CREATE TABLE IF NOT EXISTS {database}.zone_counts (
  period_start     date,
  pickup_zone_id   int,
  trips_requested  bigint,
  trips_completed  bigint,
  fare_total       double
)
LOCATION 's3://{bucket}/lake/zone_counts/'
TBLPROPERTIES ('table_type' = 'ICEBERG', 'format' = 'parquet', 'write_compression' = 'zstd')

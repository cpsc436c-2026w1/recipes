"""The Glue job: load the staged objects that no finished run has loaded into the Iceberg table, then write the
manifests. Uploaded to s3://<bucket>/jobs/s3-event-etl/etl_job.py by `scripts/etl.py up`.

It follows the Glue job of A3: getResolvedOptions, GlueContext, job.init and job.commit, and the `glue_catalog`
settings passed as --conf in the job definition. For each open day it lists stage/trip-events/dt=<day>/, reads
lake/etl-state/dt=<day>.txt and keeps the staged keys no manifest lists; reads them with the delivery schema (not
inferred), keeps each event_id once (the earliest received_time), and runs one MERGE INTO that inserts only the
event_ids the table does not hold. Only after that commit does it append the loaded keys to each day's manifest.

The functions above `main()` need boto3 only: the offline stand-in (local_load.py) and the Athena path use them too.
"""
from __future__ import annotations

import json

STAGE = "stage/trip-events/"
MANIFESTS = "lake/etl-state/"
TABLE = "trip_events_clean"
COLUMNS = [("event_id", "string"), ("event_type", "string"), ("trip_id", "string"), ("pickup_zone_id", "bigint"),
           ("driver_id", "string"), ("dropoff_zone_id", "bigint"), ("trip_miles", "double"),
           ("trip_time_s", "bigint"), ("fare", "double"), ("driver_pay", "double"), ("event_time", "timestamp"),
           ("event_day", "date"), ("delivery_id", "string"), ("received_time", "timestamp"),
           ("source_key", "string")]


def manifest_key(day: str) -> str:
    return f"{MANIFESTS}dt={day}.txt"


def _keys(s3, bucket: str, prefix: str) -> list[str]:
    out = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        out += [o["Key"] for o in page.get("Contents", [])]
    return out


def manifest(s3, bucket: str, day: str) -> dict[str, str]:
    """key -> merged, from the day's manifest (one line per key: the key, a tab, `merged`)."""
    try:
        text = s3.get_object(Bucket=bucket, Key=manifest_key(day))["Body"].read().decode()
    except s3.exceptions.NoSuchKey:
        return {}
    return dict(line.split("\t", 1) for line in text.splitlines() if "\t" in line)


def waiting(s3, bucket: str, days: list[str]) -> dict[str, list[str]]:
    """The staged keys of each open day that no manifest lists."""
    out = {}
    for day in days:
        done = manifest(s3, bucket, day)
        keys = [k for k in _keys(s3, bucket, f"{STAGE}dt={day}/") if k not in done]
        if keys:
            out[day] = sorted(keys)
    return out


def append_manifest(s3, bucket: str, day: str, keys: list[str]) -> None:
    """One PUT a day: the old lines and the newly loaded keys."""
    done = manifest(s3, bucket, day)
    done.update({k: "merged" for k in keys})
    body = "".join(f"{k}\t{v}\n" for k, v in sorted(done.items()))
    s3.put_object(Bucket=bucket, Key=manifest_key(day), Body=body.encode())


def rows_of(text: str, key: str) -> list[dict]:
    """The deliveries of a staged object, each with its source key."""
    return [{**json.loads(line), "source_key": key} for line in text.splitlines() if line.strip()]


def first_per_event(rows: list[dict]) -> list[dict]:
    """Each event_id once: the delivery received first."""
    best: dict[str, dict] = {}
    for r in rows:
        if r["event_id"] not in best or r["received_time"] < best[r["event_id"]]["received_time"]:
            best[r["event_id"]] = r
    return list(best.values())


def athena_table_ddl(database: str, bucket: str) -> str:
    """ENGINE=athena: the Iceberg table, created by Athena (with Glue, the job's CREATE TABLE IF NOT EXISTS makes it;
    without this, the first MERGE found no table: live run, 2026-11-23). Same columns, partition and location."""
    cols = ", ".join(f"{c} {t}" for c, t in COLUMNS)
    return (f"CREATE TABLE IF NOT EXISTS {database}.{TABLE} ({cols}) PARTITIONED BY (event_day) "
            f"LOCATION 's3://{bucket}/lake/{TABLE}/' TBLPROPERTIES ('table_type' = 'ICEBERG')")


def merge_sql(database: str, source: str, event_days: list[str], catalog: str = "glue_catalog") -> str:
    """Insert only the event_ids the table does not hold; the table read only on the named days."""
    days = ", ".join(f"DATE '{d}'" for d in sorted(event_days))
    cols = ", ".join(c for c, _ in COLUMNS)
    return (f"MERGE INTO {catalog}.{database}.{TABLE} t USING {source} s\n"
            f"ON t.event_id = s.event_id AND t.event_day IN ({days})\n"
            f"WHEN NOT MATCHED THEN INSERT ({cols}) VALUES ({', '.join('s.' + c for c, _ in COLUMNS)})")


def main() -> None:                                    # pragma: no cover - runs in Glue only
    import sys

    import boto3
    from awsglue.context import GlueContext
    from awsglue.job import Job
    from awsglue.utils import getResolvedOptions
    from pyspark.context import SparkContext
    from pyspark.sql import Window, functions as F, types as T

    args = getResolvedOptions(sys.argv, ["JOB_NAME", "BUCKET", "OPEN_DAYS", "RUN_ID", "DATABASE"])
    fail_after_merge = "--FAIL_AFTER_MERGE" in sys.argv      # control.FAULTS: the live scenarios of R60, R61
    if "--HANG" in sys.argv:
        import time
        time.sleep(24 * 3600)                                # ended by the run's timeout (TIMEOUT)
    glue = GlueContext(SparkContext.getOrCreate())
    spark = glue.spark_session
    job = Job(glue)
    job.init(args["JOB_NAME"], args)
    s3 = boto3.client("s3")
    bucket, db = args["BUCKET"], args["DATABASE"]
    todo = waiting(s3, bucket, args["OPEN_DAYS"].split(","))
    step = "load"
    try:
        spark.sql(f"CREATE TABLE IF NOT EXISTS glue_catalog.{db}.{TABLE} ("
                  + ", ".join(f"{c} {t}" for c, t in COLUMNS)
                  + f") USING iceberg PARTITIONED BY (event_day) LOCATION 's3://{bucket}/lake/{TABLE}/' "
                    "TBLPROPERTIES ('format-version' = '2')")
        keys = [k for ks in todo.values() for k in ks]
        if keys:
            schema = T.StructType([T.StructField(c, {"string": T.StringType(), "bigint": T.LongType(),
                                                     "double": T.DoubleType()}.get(t, T.StringType()))
                                   for c, t in COLUMNS if c not in ("event_day", "source_key")])
            df = (spark.read.schema(schema).json([f"s3://{bucket}/{k}" for k in keys])
                  .withColumn("source_key", F.regexp_replace(F.input_file_name(), f"^s3://{bucket}/", ""))
                  .withColumn("event_time", F.to_timestamp("event_time"))
                  .withColumn("received_time", F.to_timestamp("received_time"))
                  .withColumn("event_day", F.to_date("event_time")))
            first = Window.partitionBy("event_id").orderBy("received_time")
            df = df.withColumn("n", F.row_number().over(first)).where("n = 1").drop("n")
            df.createOrReplaceTempView("staged")
            event_days = [r[0].isoformat() for r in df.select("event_day").distinct().collect()]
            spark.sql(merge_sql(db, "staged", event_days))
        step = "manifest"
        if fail_after_merge:
            raise RuntimeError("FAIL_AFTER_MERGE: stopped after the commit, before the manifest")
        for day, ks in todo.items():
            append_manifest(s3, bucket, day, ks)
    except Exception as err:
        raise RuntimeError(f"step={step}: {err}") from err
    job.commit()


if __name__ == "__main__":
    main()

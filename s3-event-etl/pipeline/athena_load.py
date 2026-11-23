"""ENGINE=athena: the sweep loads with one Athena MERGE INTO in <PREFIX>-wg, then writes the same manifests.

The source is the Glue table <prefix>_lake.trip_events_stage over stage/trip-events/ (JSON Lines, partition dt by
projection), read only on the days and the "$path" objects that are waiting. Athena MERGE INTO on an Iceberg table is
what students ran in A3 (merge_members.sql).
"""
from __future__ import annotations

import time

import control
import etl_job

QUERY_TIMEOUT_S = 300


def source_key_sql(bucket: str) -> str:
    """The staged object's key without s3://<bucket>/, as the Glue job and the offline load write source_key."""
    return f'substr("$path", {len(f"s3://{bucket}/") + 1})'


def source_sql(database: str, bucket: str, todo: dict[str, list[str]]) -> str:
    days = ", ".join(f"'{d}'" for d in todo)
    paths = ", ".join(f"'s3://{bucket}/{k}'" for ks in todo.values() for k in ks)
    return (f"(SELECT event_id, event_type, trip_id, CAST(pickup_zone_id AS bigint) AS pickup_zone_id, driver_id, "
            f"CAST(dropoff_zone_id AS bigint) AS dropoff_zone_id, CAST(trip_miles AS double) AS trip_miles, "
            f"CAST(trip_time_s AS bigint) AS trip_time_s, CAST(fare AS double) AS fare, "
            f"CAST(driver_pay AS double) AS driver_pay, "
            f"CAST(from_iso8601_timestamp(event_time) AS timestamp(6)) AS event_time, "
            f"CAST(from_iso8601_timestamp(event_time) AS date) AS event_day, delivery_id, "
            f"CAST(from_iso8601_timestamp(received_time) AS timestamp(6)) AS received_time, source_key "
            f"FROM (SELECT *, {source_key_sql(bucket)} AS source_key, row_number() OVER (PARTITION BY event_id "
            f"ORDER BY received_time) AS n FROM {database}.trip_events_stage "
            f"WHERE dt IN ({days}) AND \"$path\" IN ({paths})) WHERE n = 1)")


def event_days_of(todo: dict[str, list[str]]) -> list[str]:
    """The days of the table the MERGE reads: each staged (arrival) day and the 2 days before it, since an event
    arrives at most 48 hours after its event_time (dt is the arrival day, event_day the day of event_time)."""
    from datetime import date, timedelta
    return sorted({(date.fromisoformat(d) - timedelta(days=k)).isoformat() for d in todo for k in range(3)})


def load(sess, bucket: str, days: list[str], run_id: str, log=print) -> None:
    s3, athena, client = sess.client("s3"), sess.client("athena"), control.ddb(sess)
    database = control.prefix().replace("-", "_") + "_lake"
    todo = etl_job.waiting(s3, bucket, days)
    n = sum(len(v) for v in todo.values())
    if not todo:
        control.record_run(client, run_id, "finished", objects=0, ended_at=control.iso(control.now()))
        return
    event_days = event_days_of(todo)
    sql = etl_job.merge_sql(database, source_sql(database, bucket, todo), event_days, catalog="awsdatacatalog")
    qid = athena.start_query_execution(QueryString=sql, WorkGroup=f"{control.prefix()}-wg",
                                       QueryExecutionContext={"Database": database})["QueryExecutionId"]
    control.record_run(client, run_id, "running", glue_run_id=f"athena:{qid}", objects=n)
    deadline = time.time() + QUERY_TIMEOUT_S
    while True:
        q = athena.get_query_execution(QueryExecutionId=qid)["QueryExecution"]
        state = q["Status"]["State"]
        if state in ("SUCCEEDED", "FAILED", "CANCELLED") or time.time() > deadline:
            break
        time.sleep(2)
    if state != "SUCCEEDED":
        reason = q["Status"].get("StateChangeReason", f"still {state} after {QUERY_TIMEOUT_S} s")
        import sweep
        sweep.failed(sess, run_id, "load", reason, log)
        return
    for day, keys in todo.items():
        etl_job.append_manifest(s3, bucket, day, keys)
    control.record_run(client, run_id, "finished", objects=n, ended_at=control.iso(control.now()))
    log(f"finished run {run_id}: Athena MERGE of {n} objects, "
        f"{q.get('Statistics', {}).get('DataScannedInBytes', 0):,} bytes scanned")

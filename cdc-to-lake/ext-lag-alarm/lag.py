"""Freshness: measure the lag from a change in DynamoDB to a row Athena can query, alarm when it passes a limit,
and publish a "data last updated" value a dashboard can show.

  uv run python ext-lag-alarm/lag.py                 offline: moto CloudWatch, local lake, a simulated clock
  uv run python ext-lag-alarm/lag.py --target aws    writes a probe trip, waits for it in Athena, puts metrics

Two signals, because each misses something the other sees:
  ChangeToQueryableSeconds  a probe item's whole path: stream, Lambda, Firehose buffer, Iceberg commit, Athena.
                            Measured only when the probe runs.
  IteratorAge (AWS/Lambda)  how old the newest record was when Lambda read it: the stream side only, but
                            published by Lambda on every batch at no extra charge.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from main.config import Settings, sql  # noqa: E402

NAMESPACE = "CdcToLake"
METRIC = "ChangeToQueryableSeconds"


def alarm_definitions(s: Settings) -> list[dict]:
    """PutMetricAlarm arguments. The limit is LAG_LIMIT_SECONDS (default 300 s, ours: five times a 60 s buffer)."""
    return [
        {
            "AlarmName": f"{s.prefix}-cdc-lag",
            "AlarmDescription": "A change took longer than the limit to become queryable in Athena",
            "Namespace": NAMESPACE, "MetricName": METRIC,
            "Dimensions": [{"Name": "Table", "Value": s.lake_table}],
            "Statistic": "Maximum", "Period": 300, "EvaluationPeriods": 1, "DatapointsToAlarm": 1,
            "Threshold": float(s.lag_limit_seconds), "ComparisonOperator": "GreaterThanThreshold",
            # No probe in the period means we do not know the lag: show INSUFFICIENT_DATA, not OK.
            "TreatMissingData": "missing",
        },
        {
            "AlarmName": f"{s.prefix}-cdc-iterator-age",
            "AlarmDescription": "The Lambda function is falling behind the DynamoDB stream",
            "Namespace": "AWS/Lambda", "MetricName": "IteratorAge",
            "Dimensions": [{"Name": "FunctionName", "Value": s.function}],
            "Statistic": "Maximum", "Period": 60, "EvaluationPeriods": 5, "DatapointsToAlarm": 3,
            "Threshold": 60_000.0, "ComparisonOperator": "GreaterThanThreshold",  # milliseconds; ours
            "TreatMissingData": "notBreaching",  # no batches means no records waiting
        },
    ]


def put_alarms(cw, s: Settings) -> list[str]:
    for a in alarm_definitions(s):
        cw.put_metric_alarm(**a)
    return [a["AlarmName"] for a in alarm_definitions(s)]


def alarm_state(alarm: dict, datapoints: list[float]) -> str:
    """What CloudWatch decides for the last EvaluationPeriods datapoints: ALARM if DatapointsToAlarm breach.

    Used offline to show the alarm fires; on AWS, CloudWatch evaluates the alarm itself.
    """
    window = datapoints[-alarm["EvaluationPeriods"]:]
    if not window:
        return "INSUFFICIENT_DATA"
    breaching = sum(1 for v in window if v > alarm["Threshold"])
    return "ALARM" if breaching >= alarm["DatapointsToAlarm"] else "OK"


def put_lag(cw, s: Settings, lag_s: float, at: dt.datetime) -> None:
    cw.put_metric_data(Namespace=NAMESPACE, MetricData=[{
        "MetricName": METRIC, "Dimensions": [{"Name": "Table", "Value": s.lake_table}],
        "Timestamp": at, "Value": lag_s, "Unit": "Seconds"}])


def freshness_doc(last_change, lag_s: float | None, checked_at: dt.datetime) -> dict:
    """What a dashboard reads to show "data last updated" beside its numbers (S3 object, JSON)."""
    if isinstance(last_change, str):
        last_change = dt.datetime.fromisoformat(last_change.replace(" ", "T"))
    return {
        "table": "trip_changes",
        "data_last_updated": last_change.replace(tzinfo=dt.UTC).isoformat() if last_change else None,
        "change_to_queryable_s": lag_s,
        "checked_at": checked_at.isoformat(),
    }


# --- offline ------------------------------------------------------------------------------------------------

def run_local(s: Settings, root: Path, buffer_s: float = 60.0, slow_extra_s: float = 400.0) -> str:
    """Two probes on a simulated clock: one through a healthy pipeline, one through a stuck one."""
    import boto3
    from moto import mock_aws

    from main.handler import handler
    from main.lake import LocalFirehose, LocalLake

    from main.config import offline_env

    offline_env(s.region)
    now = [1_800_000_000.0]
    clock = lambda: now[0]  # noqa: E731
    lake = LocalLake(root, s)
    lines = []
    with mock_aws():
        cw = boto3.client("cloudwatch", region_name=s.region)
        put_alarms(cw, s)
        lags = []
        for label, extra in (("healthy", 0.0), ("stuck for a while", slow_extra_s)):
            probe = f"probe-{uuid.uuid4().hex[:8]}"
            changed = clock()
            event = {"Records": [{"eventName": "INSERT", "dynamodb": {
                "ApproximateCreationDateTime": int(changed), "SequenceNumber": str(int(changed)),
                "Keys": {"trip_id": {"S": probe}},
                "NewImage": {"trip_id": {"S": probe}, "status": {"S": "probe"}}}}]}
            now[0] += 2 + extra  # the stream and Lambda; "stuck" adds the time the function was failing
            firehose = LocalFirehose(lake, buffer_s, clock=clock)
            handler(event, None, firehose=firehose, stream=s.delivery_stream, clock=clock)
            while not firehose.tick():
                now[0] += 5  # the probe polls the lake every 5 s
            row = lake.query(f"SELECT change_time FROM {s.lake_table} WHERE trip_id = '{probe}'")[0]
            lag = clock() - row["change_time"].replace(tzinfo=dt.UTC).timestamp()
            lags.append(lag)
            at = dt.datetime.fromtimestamp(clock(), dt.UTC)
            put_lag(cw, s, lag, at)
            state = alarm_state(alarm_definitions(s)[0], [lag])
            lines.append(f"{label:<18} change to queryable {lag:5.0f} s   limit {s.lag_limit_seconds} s   "
                         f"alarm {state}")
        stored = cw.list_metrics(Namespace=NAMESPACE)["Metrics"]
        names = [a["AlarmName"] for a in cw.describe_alarms()["MetricAlarms"]]
    fresh = lake.query(sql("freshness", table=s.lake_table))[0]
    doc = freshness_doc(fresh["last_change"], lags[-1], dt.datetime.fromtimestamp(clock(), dt.UTC))
    lines += [f"alarms defined: {', '.join(sorted(names))}",
              f"metrics stored: {len(stored)} ({NAMESPACE}/{METRIC})",
              "freshness.json for the dashboard:", json.dumps(doc, indent=2)]
    return "[local: simulated clock, buffer 60 s]\n" + "\n".join(lines)


# --- AWS ----------------------------------------------------------------------------------------------------

def run_aws(s: Settings, wait_s: int = 900, poll_s: int = 10) -> str:
    import boto3

    from main import athena as athena_q
    from main.infra import account_id

    account = account_id(s.region)
    c = lambda svc: boto3.client(svc, region_name=s.region)  # noqa: E731
    cw = c("cloudwatch")
    put_alarms(cw, s)
    probe = f"probe-{uuid.uuid4().hex[:8]}"
    table = boto3.resource("dynamodb", region_name=s.region).Table(s.table)
    table.put_item(Item={"trip_id": probe, "status": "probe"})
    athena, out = c("athena"), s.athena_output(account)
    deadline = time.time() + wait_s
    while True:
        res = athena_q.run(athena, f"SELECT change_time FROM {s.lake_table} WHERE trip_id = '{probe}'",
                           s.workgroup, out, s.database)
        if res.rows:
            break
        if time.time() > deadline:
            raise TimeoutError(f"probe {probe} not queryable after {wait_s} s")
        time.sleep(poll_s)
    seen = time.time()
    changed = dt.datetime.fromisoformat(res.rows[0]["change_time"].replace(" ", "T")).replace(tzinfo=dt.UTC)
    lag = seen - changed.timestamp()  # change_time has whole seconds, and the poll interval adds up to poll_s
    put_lag(cw, s, lag, dt.datetime.now(dt.UTC))
    table.delete_item(Key={"trip_id": probe})  # its REMOVE reaches the lake too; trips_current leaves it out
    fresh = athena_q.run(athena, sql("freshness", table=s.lake_table), s.workgroup, out, s.database).rows[0]
    doc = freshness_doc(fresh["last_change"], round(lag, 1), dt.datetime.now(dt.UTC))
    c("s3").put_object(Bucket=s.bucket(account), Key="freshness/trip_changes.json",
                       Body=json.dumps(doc).encode(), ContentType="application/json")
    return (f"probe {probe}: change to queryable {lag:.0f} s (limit {s.lag_limit_seconds} s)\n"
            f"freshness written to s3://{s.bucket(account)}/freshness/trip_changes.json\n{json.dumps(doc, indent=2)}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--target", choices=["local", "aws"], default="local")
    args = p.parse_args()
    s = Settings()
    if args.target == "aws":
        print(run_aws(s))
    else:
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            print(run_local(s, Path(d)))


if __name__ == "__main__":
    main()

"""Main scenario: write trips to DynamoDB, change one, remove one, and query the lake until the changes show.

  uv run python -m main.run                    offline: moto DynamoDB and Streams, local Iceberg table, DuckDB
  uv run python -m main.run --target aws       your sandbox (main/infra.py creates what it needs)

The local run feeds the same main/handler.py the Lambda function runs; only the stream reader (here a loop, on
AWS the event source mapping), Firehose and Athena are stand-ins (main/lake.py).
"""

from __future__ import annotations

import argparse
import datetime as dt
import tempfile
import time
from pathlib import Path

import boto3

from main import handler
from main.config import Settings, offline_env, sql

TRIPS = [
    {"trip_id": "t-001", "status": "requested", "pickup_zone": "Kitsilano", "fare_quote": "18.50"},
    {"trip_id": "t-002", "status": "requested", "pickup_zone": "Downtown", "fare_quote": "12.00"},
    {"trip_id": "t-003", "status": "requested", "pickup_zone": "Mount Pleasant", "fare_quote": "15.25"},
]
CHANGED = "t-001"   # a driver claims it: status and driver_id change
REMOVED = "t-003"   # the rider cancels it: the item is deleted


def write_scenario(ddb, table: str) -> None:
    from decimal import Decimal

    for t in TRIPS:
        ddb.Table(table).put_item(Item={**t, "fare_quote": Decimal(t["fare_quote"])})
    ddb.Table(table).update_item(
        Key={"trip_id": CHANGED},
        UpdateExpression="SET #s = :claimed, driver_id = :d",
        ConditionExpression="#s = :requested",  # claim once (recipes/dynamodb, 03-claim-once)
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":claimed": "claimed", ":requested": "requested", ":d": "d-42"})
    ddb.Table(table).delete_item(Key={"trip_id": REMOVED})


def report(current: list[dict], fresh: dict) -> str:
    lines = ["trips_current (latest state of each trip, from the change log):"]
    for r in sorted(current, key=lambda r: r["trip_id"]):
        lines.append(f"  {r['trip_id']}  {r['status']:<10} {r['pickup_zone']:<15} {float(r['fare_quote']):6.2f}"
                     f"  driver={r['driver_id']}")
    lines.append(f"changes in the log: {fresh['changes']} for {fresh['trips']} trips")
    lines.append(f"data last updated (newest change in the lake): {fresh['last_change']}")
    lines.append(f"longest change-to-arrival: {fresh['max_change_to_arrival_s']} s")
    return "\n".join(lines)


def check(current: list[dict]) -> None:
    by_id = {r["trip_id"]: r for r in current}
    assert by_id[CHANGED]["status"] == "claimed", "the changed trip is not in the lake yet"
    assert REMOVED not in by_id, "the removed trip still shows"


# --- offline -------------------------------------------------------------------------------------------------

def stream_events(streams, stream_arn: str, iterators: dict) -> list[dict]:
    """Read new stream records and shape them as Lambda receives them (what the event source mapping does)."""
    shards = streams.describe_stream(StreamArn=stream_arn)["StreamDescription"]["Shards"]
    records = []
    for sh in shards:
        it = iterators.get(sh["ShardId"]) or streams.get_shard_iterator(
            StreamArn=stream_arn, ShardId=sh["ShardId"], ShardIteratorType="TRIM_HORIZON")["ShardIterator"]
        resp = streams.get_records(ShardIterator=it)
        iterators[sh["ShardId"]] = resp["NextShardIterator"]
        for r in resp["Records"]:
            d = dict(r["dynamodb"])
            d["ApproximateCreationDateTime"] = int(d["ApproximateCreationDateTime"].timestamp())  # whole seconds
            records.append({**r, "dynamodb": d})
    return records


def run_local(s: Settings, root: Path, buffer_s: float = 2.0) -> str:
    from moto import mock_aws

    from main.lake import LocalFirehose, LocalLake

    offline_env(s.region)
    with mock_aws():
        from main.infra import create_table

        stream_arn = create_table(boto3.client("dynamodb", region_name=s.region), s)
        write_scenario(boto3.resource("dynamodb", region_name=s.region), s.table)
        lake = LocalLake(root, s)
        firehose = LocalFirehose(lake, buffer_s)
        events = stream_events(boto3.client("dynamodbstreams", region_name=s.region), stream_arn, {})
        out = handler.handler({"Records": events}, None, firehose=firehose, stream=s.delivery_stream)
        assert out["batchItemFailures"] == []
        before = lake.query(f"SELECT count(*) AS n FROM {s.lake_table}")[0]["n"]
        while not firehose.tick():  # the buffer interval passes before anything is queryable
            time.sleep(0.2)
        current = lake.query(f"SELECT * FROM {s.view}")
        fresh = lake.query(sql("freshness", table=s.lake_table))[0]
        check(current)
        return (f"[local stand-ins: moto, Iceberg on disk, DuckDB; buffer {buffer_s} s]\n"
                f"rows queryable before the buffer was written: {before}\n" + report(current, fresh))


# --- AWS -----------------------------------------------------------------------------------------------------

def run_aws(s: Settings, wait_s: int = 900) -> str:
    from main import athena as athena_q
    from main.infra import create_all

    info = create_all(s)
    write_scenario(boto3.resource("dynamodb", region_name=s.region), s.table)
    changed_at = time.time()
    athena = boto3.client("athena", region_name=s.region)
    out = s.athena_output(info["account"])
    deadline = time.time() + wait_s
    while True:
        res = athena_q.run(athena, f"SELECT * FROM {s.view}", s.workgroup, out, s.database)
        try:
            check(res.rows)
            break
        except (AssertionError, KeyError):
            if time.time() > deadline:
                raise TimeoutError(f"changes not in the lake after {wait_s} s; check the Lambda logs and "
                                   f"s3://{info['bucket']}/firehose-errors/")
            time.sleep(15)
    seen_s = time.time() - changed_at
    fresh = athena_q.run(athena, sql("freshness", table=s.lake_table), s.workgroup, out, s.database)
    return (f"[aws {s.region}; buffer {s.buffer_seconds} s; changes queryable after about {seen_s:.0f} s "
            f"(polled every 15 s); Data scanned by the last query: {fresh.data_scanned_bytes} bytes]\n"
            + report(res.rows, fresh.rows[0]))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--target", choices=["local", "aws"], default="local")
    args = p.parse_args()
    s = Settings()
    if args.target == "aws":
        print(run_aws(s))
    else:
        with tempfile.TemporaryDirectory() as d:
            print(run_local(s, Path(d)))
    print(f"run at {dt.datetime.now(dt.UTC):%Y-%m-%d %H:%M} UTC")


if __name__ == "__main__":
    main()

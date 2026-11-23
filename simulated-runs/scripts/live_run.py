"""The recipe's own live run: a short replay into the lake bucket and into a 1-shard Kinesis stream, read back and
compared with the answer from _truth/ (sim/truth.py), the stream deleted right after.

    PREFIX=recipe-you uv run python scripts/lake.py up
    PREFIX=recipe-you uv run python scripts/live_run.py stream-up        # 1 provisioned shard, tagged, in the ledger
    PREFIX=recipe-you uv run python scripts/live_run.py run --to kinesis --speed 60 --run-id <id>
    PREFIX=recipe-you uv run python scripts/live_run.py run --to s3 --speed 0 --run-id <id>
    PREFIX=recipe-you uv run python scripts/live_run.py stream-down      # the shard bills by the hour: delete it now

"The deployed system" here is what a team's pipeline reads: the raw slot files under raw/trip-events/ and the
records on the stream. `run` reads them back, keeps each event_id once, computes the requested-per-zone table with
the same DuckDB SQL as offline, compares it with the answer from _truth/ and writes evidence/<run-id>/ (sent.jsonl, the
command, the commit, tables.json). A team's own @live step reads its own deployed tables instead.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path[:0] = [str(HERE), str(ROOT / "sim")]
import generate  # noqa: E402
import replay  # noqa: E402
import truth  # noqa: E402
from tags import prefix, record, recorded, tags  # noqa: E402

REGION = "ca-central-1"


def stream_name() -> str:
    return f"{prefix()}-trip-events"


def stream_up(sess, log=print, sleep=time.sleep) -> str:
    kin, name = sess.client("kinesis"), stream_name()
    try:
        kin.describe_stream_summary(StreamName=name)
        if recorded("kinesis-stream", name) != "created":
            raise RuntimeError(f"stream {name} exists and this ledger did not create it")
    except ClientError as err:
        if err.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
        kin.create_stream(StreamName=name, ShardCount=1, StreamModeDetails={"StreamMode": "PROVISIONED"},
                          Tags=tags("kinesis"))
        arn = kin.describe_stream_summary(StreamName=name)["StreamDescriptionSummary"]["StreamARN"]
        record("kinesis-stream", name, "created", arn=arn, script="scripts/live_run.py stream-up",
               clean="scripts/live_run.py stream-down, clean_up.py", bills="hourly")
        log(f"created  Kinesis stream {name} (1 provisioned shard, billed by the hour until deleted)")
    kin.get_waiter("stream_exists").wait(StreamName=name, WaiterConfig={"Delay": 5, "MaxAttempts": 36})
    return name


def stream_down(sess, log=print) -> None:
    kin, name = sess.client("kinesis"), stream_name()
    try:
        kin.delete_stream(StreamName=name, EnforceConsumerDeletion=True)
        record("kinesis-stream", name, "deleted", script="scripts/live_run.py stream-down")
        log(f"deleting Kinesis stream {name}")
        kin.get_waiter("stream_not_exists").wait(StreamName=name, WaiterConfig={"Delay": 5, "MaxAttempts": 36})
        log(f"removed  Kinesis stream {name} (describe-stream: not found)")
    except ClientError as err:
        if err.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
        log(f"absent   Kinesis stream {name}")


def read_back_kinesis(sess, run_id: str, expect: int, timeout_s: float = 120, sleep=time.sleep) -> list[dict]:
    """Every record of this run on the stream, from the start of its one shard."""
    kin, name = sess.client("kinesis"), stream_name()
    shards = kin.list_shards(StreamName=name)["Shards"]
    out = []
    for shard in shards:
        it = kin.get_shard_iterator(StreamName=name, ShardId=shard["ShardId"],
                                    ShardIteratorType="TRIM_HORIZON")["ShardIterator"]
        deadline = time.monotonic() + timeout_s
        while it and time.monotonic() < deadline:
            page = kin.get_records(ShardIterator=it, Limit=10000)
            out += [json.loads(r["Data"]) for r in page["Records"]]
            mine = [d for d in out if d["trip_id"].startswith(f"{run_id}-")]
            if len(mine) >= expect or (not page["Records"] and page.get("MillisBehindLatest", 0) == 0
                                       and len(mine) > 0):
                break
            it = page.get("NextShardIterator")
            sleep(0.25)                        # GetRecords: 5 calls a second per shard
    return [d for d in out if d["trip_id"].startswith(f"{run_id}-")]


def read_back_s3(sess, run_id: str) -> list[dict]:
    """Every line of this run's slot files under raw/trip-events/."""
    s3 = sess.client("s3")
    bucket = f"{prefix()}-lake-{sess.client('sts').get_caller_identity()['Account']}"
    out = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix="raw/trip-events/"):
        for obj in page.get("Contents", []):
            if obj["Key"].endswith(f"-{run_id}.jsonl"):
                body = s3.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read().decode()
                out += [json.loads(line) for line in body.splitlines() if line.strip()]
    return out


def tables(received: list[dict], run_id: str) -> dict:
    """The deployed system's answer from what it received: each event_id once, run id taken off."""
    from rides.pipeline import fare_by_trip, requested_per_zone
    events = {}
    for d in received:
        e = {k: v for k, v in d.items() if k not in ("delivery_id", "received_time")}
        e["event_id"], e["trip_id"] = replay.unprefixed(e["event_id"], run_id), replay.unprefixed(e["trip_id"], run_id)
        events[e["event_id"]] = e
    rows = list(events.values())
    return {"deliveries_received": len(received), "events": len(rows),
            "trips": len({e["trip_id"] for e in rows}),
            "requested_per_zone": {str(k): v for k, v in requested_per_zone(rows).items()},
            "fare_by_trip": fare_by_trip(rows)}


def run(sess, data_set: str, to: str, speed: float, run_id: str, log=print) -> dict:
    """Replay, read back, compare with the answer from _truth/; evidence in evidence/<run-id>/."""
    folder = generate.ensure(data_set)
    deliveries = replay.load_deliveries(data_set)
    evidence = ROOT / "evidence" / run_id
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / "command.txt").write_text(
        f"scripts/live_run.py run {data_set} --to {to} --speed {speed} --run-id {run_id}\ncommit {replay._commit()}\n")
    sender = replay.make_sender(to, run_id=run_id, session=sess)
    if to == "s3":     # clean_up.py deletes the slot files of the run ids recorded here, and no other files
        record("s3-replay", run_id, "created", script="scripts/live_run.py run", clean="clean_up.py")
    summary = replay.replay(deliveries, sender, run_id, speed, evidence=evidence,
                            shift_to=datetime.now(timezone.utc))
    log(f"sent     {summary['sent']} deliveries to {summary['target']} in {summary['calls']} calls, "
        f"{summary['seconds']} s; latest send {summary['late_s']} s after its planned time; {summary['failed']} failed")
    received = read_back_kinesis(sess, run_id, len(deliveries)) if to == "kinesis" else read_back_s3(sess, run_id)
    answer = truth.answer(folder)
    got = tables(received, run_id)
    got["matches_truth"] = (got["requested_per_zone"] == answer["requested_per_zone"]
                            and got["events"] == answer["events"] and got["trips"] == answer["trips"])
    (evidence / "tables.json").write_text(json.dumps({**{k: v for k, v in got.items() if k != "fare_by_trip"},
                                                      "truth": {k: answer[k] for k in ("events", "trips",
                                                                                       "requested_per_zone")}},
                                                     indent=1, sort_keys=True) + "\n")
    log(f"read     {got['deliveries_received']} deliveries back, {got['events']} events once each, "
        f"{got['trips']} trips; requested per zone {got['requested_per_zone']}; truth "
        f"{answer['requested_per_zone']}: {'same' if got['matches_truth'] else 'DIFFERENT'}")
    return {**summary, **got}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("stream-up")
    sub.add_parser("stream-down")
    r = sub.add_parser("run")
    r.add_argument("data_set", nargs="?", default="SIM01")
    r.add_argument("--to", choices=["kinesis", "s3"], required=True)
    r.add_argument("--speed", type=float, default=0)
    r.add_argument("--run-id", required=True)
    args = p.parse_args(argv)
    sess = boto3.session.Session(region_name=REGION)
    if args.cmd == "stream-up":
        stream_up(sess)
    elif args.cmd == "stream-down":
        stream_down(sess)
    else:
        result = run(sess, args.data_set, args.to, args.speed, args.run_id)
        return 0 if result["matches_truth"] and not result["failed"] else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

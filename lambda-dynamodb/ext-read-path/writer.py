"""The second process of `run.sh --busy-rider --writer 5`: SIM04's updates of the busy rider's trips, at a fixed rate.

    python3 ext-read-path/writer.py --table recipe-rider-trips --rate 5       # prints one JSON line at the end

Each update sets `rating` (1 to 5) and `rated_time` on one of the 300 trips of rider r-busy, one per trip in an order
shuffled with seed 436; no update is sent earlier than its slot (update i at i / rate seconds after the start). The
client retries in botocore's adaptive mode, at most 10 attempts, which also slows the writer when DynamoDB throttles
it. A send also waits until the send `rate` places back is a second old. It counts the throttled answers and the updates that needed a retry, and prints them with the number done.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from datetime import datetime, timedelta, timezone

THROTTLED = {"ProvisionedThroughputExceededException", "ThrottlingException", "RequestLimitExceeded",
             "ThrottlingError"}
BUSY_RIDER = "r-busy"
FIRST = datetime(2026, 10, 1, tzinfo=timezone.utc)


def busy_trips() -> list[dict]:
    """SIM03: 300 trips of r-busy, one every 10 minutes from 2026-10-01T00:00:00Z, about 75 bytes each."""
    return [{"rider_id": BUSY_RIDER, "requested_at": (FIRST + timedelta(minutes=10 * i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
             "trip_id": f"b-{i:04d}", "pickup_zone": "Midtown"} for i in range(300)]


def updates(seed: int = 436) -> list[dict]:
    """SIM04: one update per busy-rider trip, shuffled with the seed."""
    rng = random.Random(seed)
    trips = busy_trips()
    rng.shuffle(trips)
    return [{"requested_at": t["requested_at"], "rating": rng.randint(1, 5),
             "rated_time": (datetime.fromisoformat(t["requested_at"].replace("Z", "+00:00")) + timedelta(hours=1))
             .strftime("%Y-%m-%dT%H:%M:%SZ")} for t in trips]


def client(sess=None):
    import boto3
    from botocore.config import Config
    sess = sess or boto3.session.Session(region_name="ca-central-1")
    return sess.client("dynamodb", config=Config(retries={"mode": "adaptive", "max_attempts": 10}))


def run(ddb, table: str, rate: float, clock=time.monotonic, sleep=time.sleep) -> dict:
    counts = {"updates_done": 0, "throttled": 0, "retried": 0, "failed": 0, "sent_at": []}

    def seen(response=None, **_):         # every answer, retries included: count the throttled ones
        if response and isinstance(response[1], dict):
            if response[1].get("Error", {}).get("Code") in THROTTLED:
                counts["throttled"] += 1

    ddb.meta.events.register_first("needs-retry.dynamodb.UpdateItem", seen)   # before the retry handler
    start, window = clock(), max(1, int(rate))
    for i, u in enumerate(updates()):
        # not before its slot, and not before the send `rate` sends back is a second old: so no one-second window
        # ever holds more than `rate` sends, also after a slow answer
        due = start + i / rate
        if i >= window:
            due = max(due, start + counts["sent_at"][i - window] + 1)
        pause = due - clock()
        if pause > 0:
            sleep(pause)
        counts["sent_at"].append(clock() - start)
        try:
            r = ddb.update_item(TableName=table, Key={"rider_id": {"S": BUSY_RIDER}, "requested_at": {"S": u["requested_at"]}},
                                UpdateExpression="SET rating = :r, rated_time = :t",
                                ConditionExpression="attribute_exists(rider_id)",
                                ExpressionAttributeValues={":r": {"N": str(u["rating"])}, ":t": {"S": u["rated_time"]}})
            counts["updates_done"] += 1
            if r["ResponseMetadata"].get("RetryAttempts", 0):
                counts["retried"] += 1
        except Exception:
            counts["failed"] += 1
    return counts


def max_per_second(sent_at: list[float]) -> int:
    """The most updates sent in any one-second window [t, t + 1)."""
    return max((sum(1 for u in sent_at if t <= u < t + 1) for t in sent_at), default=0)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--table", required=True)
    p.add_argument("--rate", type=float, default=5.0)
    a = p.parse_args(argv)
    out = run(client(), a.table, a.rate)
    out["max_per_second"] = max_per_second(out.pop("sent_at"))
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())

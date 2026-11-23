"""Land SIM01 in the lake bucket in AWS as the live scenarios want it: SIM02's three broken lines in the 09:04
object (the 5th minute of the replay), and, with --again, SIM03's copy of the 09:30 object under `…-again.jsonl`.

    uv run python sim/land_cases.py --run-id <id> --bucket "$PREFIX-lake-$ACCOUNT_ID"            # SIM01 + SIM02's lines
    uv run python sim/land_cases.py --run-id <id> --bucket <bucket> --again                   # then SIM03's copy

The lines are broken as features/steps/steps.py breaks them offline (an extra field `tip`, an `event_time` without a
time zone, no `trip_id`), each in place of a delivery sent once, so the table then holds 2,508 rows, not 2,511.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "sim"))
import replay  # noqa: E402

BROKEN_SLOT = 4          # the 09:04 object: 4 minutes after the first
AGAIN_SLOT = 30          # the 09:30 object


def broken(lines: list[str], counts: Counter) -> list[str]:
    once = [i for i, ln in enumerate(lines) if counts[json.loads(ln)["event_id"]] == 1][:3]
    out = list(lines)
    for i, how in zip(once, ("tip", "event_time", "trip_id")):
        d = json.loads(out[i])
        if how == "tip":
            d["tip"] = 2.5
        elif how == "event_time":
            d["event_time"] = d["event_time"].removesuffix("Z").split(".")[0]
        else:
            d.pop("trip_id")
        out[i] = json.dumps(d, sort_keys=True)
    return out


class BreakingClient:
    """Passes put_object through, with the lines of one slot's object broken."""

    def __init__(self, client, counts: Counter):
        self.client, self.counts, self.n, self.keys = client, counts, 0, []

    def put_object(self, **kw):
        if self.n == BROKEN_SLOT:
            lines = kw["Body"].decode().splitlines()
            kw["Body"] = "".join(x + "\n" for x in broken(lines, self.counts)).encode()
        self.n += 1
        self.keys.append(kw["Key"])
        return self.client.put_object(**kw)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--bucket", required=True)
    ap.add_argument("--again", action="store_true", help="copy the 09:30 object of this run under …-again.jsonl")
    args = ap.parse_args(argv)
    s3 = boto3.session.Session(region_name="ca-central-1").client("s3")
    if args.again:
        keys = sorted(o["Key"] for p in s3.get_paginator("list_objects_v2").paginate(
            Bucket=args.bucket, Prefix="raw/trip-events/") for o in p.get("Contents", []) if args.run_id in o["Key"])
        src = keys[AGAIN_SLOT]
        dst = src.replace(".jsonl", "-again.jsonl")
        s3.copy_object(Bucket=args.bucket, Key=dst, CopySource={"Bucket": args.bucket, "Key": src})
        print(f"copied   {src} -> {dst}")
        return 0
    sys.path.insert(0, str(ROOT / "scripts"))
    from tags import record                 # scripts/lake.py down deletes the slot files of the run ids recorded here
    record("s3-replay", args.run_id, "created", script="sim/land_cases.py", clean="scripts/lake.py down")
    deliveries = replay.load_deliveries("SIM01")
    counts = Counter(replay.prefixed(d, args.run_id)["event_id"] for d in deliveries)
    client = BreakingClient(s3, counts)
    sender = replay.S3Sender(client, args.bucket, args.run_id)
    summary = replay.replay(deliveries, sender, args.run_id, 0.0, shift_to=datetime.now(timezone.utc))
    print(f"landed   {len(client.keys)} objects, {summary['sent']} deliveries; 3 lines of "
          f"{client.keys[BROKEN_SLOT].rsplit('/', 1)[-1]} broken")
    return 0


if __name__ == "__main__":
    sys.exit(main())

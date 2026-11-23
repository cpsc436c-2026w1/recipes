"""Land deliveries in the lake as small Parquet files, one a slot, as Firehose's record format conversion writes them.

    uv run python ext-maintenance/land_parquet.py --deliveries sim/data/SIM01/trip-events/dt=2026-11-23/hour=09/deliveries.jsonl
    uv run python ext-maintenance/land_parquet.py --days 30 --files-per-day 1440     # SIM01's hour, every day
    uv run python ext-maintenance/land_parquet.py --days 3 --out /tmp/lake           # to a local folder

Each file is `raw/trip-events/dt=<UTC day it arrived>/<slot start>-<n>.parquet` in the lake bucket
`<PREFIX>-lake-<account>`, with the columns of `trip_events_parquet` (nightly_ops.DATA_COLUMNS). `--days N` repeats
SIM01's hour of deliveries on each of the N days before today, spread over FILES_PER_DAY files, so the folders hold
as many small files as a 60 s buffer makes; its trip counts are SIM01's (837 a day), not those of a thirty-day data
set of DS01.
"""
from __future__ import annotations

import argparse
import io
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
# Appended, not put first: a folder of this recipe never hides a module of the same name in another one
# (each extension has its own clean_up.py).
sys.path += [p for p in [str(HERE), str(ROOT / "scripts"), str(ROOT / "sim"), str(ROOT / "ext-scheduled-merge"), str(ROOT)] if p not in sys.path]

from nightly_ops import DATA_COLUMNS  # noqa: E402

TYPES = {"string": "string", "int": "int32", "double": "float64"}


def parquet_bytes(rows: list[dict]) -> bytes:
    import pyarrow as pa
    import pyarrow.parquet as pq
    schema = pa.schema([(n, TYPES[t]) for n, t in DATA_COLUMNS])
    table = pa.Table.from_pylist([{n: r.get(n) for n, _ in DATA_COLUMNS} for r in rows], schema=schema)
    buf = io.BytesIO()
    pq.write_table(table, buf, compression="snappy")
    return buf.getvalue()


def slots(deliveries: list[dict], files_per_day: int) -> dict[tuple[str, int], list[dict]]:
    """(arrival day, slot number) -> its deliveries; a day has `files_per_day` slots."""
    out: dict[tuple[str, int], list[dict]] = {}
    seconds = 86400 / files_per_day
    for d in deliveries:
        t = datetime.fromisoformat(d["received_time"].replace("Z", "+00:00"))
        n = int((t.hour * 3600 + t.minute * 60 + t.second) // seconds)
        out.setdefault((t.date().isoformat(), n), []).append(d)
    return out


def spread(deliveries: list[dict], days: int, files_per_day: int, today) -> list[dict]:
    """SIM01's deliveries on each of `days` days before today, their received times spread evenly over the day so
    every slot holds a file."""
    out = []
    ordered = sorted(deliveries, key=lambda d: d["received_time"])
    step = 86400 / len(ordered)
    for k in range(days, 0, -1):
        day = datetime(today.year, today.month, today.day, tzinfo=timezone.utc) - timedelta(days=k)
        for i, d in enumerate(ordered):
            t = day + timedelta(seconds=i * step)
            stamp = t.strftime("%Y-%m-%dT%H:%M:%S.000Z")
            out.append({**d, "event_id": f"d{k:02d}-{d['event_id']}", "trip_id": f"d{k:02d}-{d['trip_id']}",
                        "delivery_id": f"d{k:02d}-{d['delivery_id']}", "received_time": stamp, "event_time": stamp})
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--deliveries", help="a deliveries.jsonl to land as it is")
    p.add_argument("--days", type=int, default=0, help="SIM01's hour on each of this many days before today")
    p.add_argument("--files-per-day", type=int, default=1440)
    p.add_argument("--out", help="a local folder instead of the lake bucket")
    args = p.parse_args(argv)
    if args.deliveries:
        deliveries = [json.loads(x) for x in Path(args.deliveries).read_text().splitlines() if x.strip()]
    else:
        import sim_data
        deliveries, _ = sim_data.load("SIM01")
        deliveries = spread(deliveries, args.days or 1, args.files_per_day, datetime.now(timezone.utc).date())
    groups = slots(deliveries, args.files_per_day)
    s3 = bucket = None
    if not args.out:
        import boto3
        import lake
        sess = boto3.session.Session(region_name="ca-central-1")
        s3, bucket = sess.client("s3"), lake.bucket_name(sess)
    if s3 is not None:                      # scripts/lake.py down deletes the files under the prefixes recorded here
        from tags import record
        for day in sorted({d for d, _ in groups}):
            record("s3-prefix", f"raw/trip-events/dt={day}/{day.replace('-', '')}-", "created",
                   script="ext-maintenance/land_parquet.py", clean="scripts/lake.py down")
    for (day, n), rows in sorted(groups.items()):
        key = f"raw/trip-events/dt={day}/{day.replace('-', '')}-{n:04d}.parquet"
        body = parquet_bytes(rows)
        if args.out:
            path = Path(args.out) / key
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
        else:
            s3.put_object(Bucket=bucket, Key=key, Body=body)
    days = sorted({d for d, _ in groups})
    print(f"landed {len(deliveries)} deliveries as {len(groups)} Parquet files over {len(days)} days "
          f"({days[0]} to {days[-1]})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

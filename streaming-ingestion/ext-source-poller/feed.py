"""The simulated partner feed: one JSON Lines file per 15-minute slot and `manifest.json` listing the published ones.

    uv run python ext-source-poller/feed.py --to local                    # feed/ in this folder, the whole day
    uv run python ext-source-poller/feed.py --to aws                      # s3://<PREFIX>-lake-<account>/feed/
    uv run python ext-source-poller/feed.py --to aws --up-to 2026-11-22T12:50:00Z
    uv run python ext-source-poller/feed.py --to aws --live --slots 8     # publish in real time, from now

The day is SIM01 `feed-day` of sim/stand_in.py (synthetic trips with the data set's counts): 96 slots of
2026-11-22, each file published 2 min after its slot ends, slot 09:15 never published, slot 10:30 published at
12:45. `--up-to T` lists in the manifest only the files published by T, as the partner's manifest is at T.

`--live` moves the day so that its first slot is the current 15-minute slot, and publishes each file at its moved
publication time (it sleeps in between), rewriting the manifest each time, so the scheduled poller meets the feed
as it would a partner's. It writes `evidence/feed-<start>/published.jsonl` with the time each file was put, which
latency.py reads. A whole day is 26 hours: run it from a laptop terminal (CloudShell ends idle sessions), or use
--slots for a shorter run.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
for folder in (ROOT / "sim", ROOT / "scripts", ROOT / "src"):
    sys.path.insert(0, str(folder))

SLOT = timedelta(minutes=15)
PREFIX = "feed/"


def parse(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def stamp(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def day_slots(data_dir: Path | None = None) -> list[dict]:
    """The 96 slots of SIM01: slot, file, published_time (or None), and the file's bytes."""
    import stand_in

    folder = stand_in.ensure("feed-day", data_dir or stand_in.DATA)
    slots = json.loads((folder / "_truth" / "slots.json").read_text())
    lines: dict[str, list[str]] = {s["slot"]: [] for s in slots}
    for text in (folder / "_truth" / "events.jsonl").read_text().splitlines():
        e = json.loads(text)
        t = parse(e["event_time"])
        start = t.replace(minute=t.minute - t.minute % 15, second=0, microsecond=0)
        lines[start.strftime("%Y-%m-%dT%H:%MZ")].append(text)
    return [{**s, "body": ("\n".join(lines[s["slot"]]) + "\n").encode()} for s in slots]


def manifest(slots: list[dict], up_to: datetime | None) -> bytes:
    listed = [{"slot": s["slot"], "file": s["file"], "published_time": s["published_time"]} for s in slots
              if s["published_time"] and (up_to is None or parse(s["published_time"]) <= up_to)]
    return json.dumps({"slots": listed}, indent=1).encode()


def publish(put, slots: list[dict], up_to: datetime | None = None, files: bool = True) -> int:
    """Write the files (all of them, or none with files=False) and the manifest as at `up_to`. `put(name, bytes)`."""
    if files:
        for s in slots:
            put(s["file"], s["body"])
    put("manifest.json", manifest(slots, up_to))
    return sum(1 for s in slots if s["published_time"] and (up_to is None or parse(s["published_time"]) <= up_to))


def s3_put(s3, bucket: str, prefix: str = PREFIX):
    def put(name: str, body: bytes) -> None:
        s3.put_object(Bucket=bucket, Key=prefix + name, Body=body,
                      ContentType="application/json" if name.endswith(".json") else "application/x-ndjson")
    return put


def shifted(slots: list[dict], to: datetime) -> list[dict]:
    first = parse(slots[0]["slot"])
    offset = to - first
    out = []
    for s in slots:
        start = parse(s["slot"]) + offset
        out.append({**s, "slot": start.strftime("%Y-%m-%dT%H:%MZ"), "file": f"trips-{start:%Y-%m-%dT%H%M}Z.jsonl",
                    "published_time": stamp(parse(s["published_time"]) + offset) if s["published_time"] else None,
                    "body": _shift_body(s["body"], offset)})
    return out


def _shift_body(body: bytes, offset: timedelta) -> bytes:
    out = []
    for text in body.decode().splitlines():
        e = json.loads(text)
        t = parse(e["event_time"]) + offset
        e["event_time"] = t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"
        out.append(json.dumps(e, sort_keys=True))
    return ("\n".join(out) + "\n").encode()


def live(s3, bucket: str, n_slots: int, sleep=time.sleep, now=lambda: datetime.now(timezone.utc)) -> Path:
    t0 = now()
    start = t0.replace(minute=t0.minute - t0.minute % 15, second=0, microsecond=0)
    slots = shifted(day_slots(), start)[:n_slots]
    evidence = ROOT / "evidence" / f"feed-{start:%Y%m%dT%H%MZ}"
    evidence.mkdir(parents=True, exist_ok=True)
    put, published = s3_put(s3, bucket), []
    put("manifest.json", manifest([], None))
    for s in sorted((s for s in slots if s["published_time"]), key=lambda s: s["published_time"]):
        pause = (parse(s["published_time"]) - now()).total_seconds()
        if pause > 0:
            sleep(pause)
        put(s["file"], s["body"])
        published.append({**{k: v for k, v in s.items() if k != "body"}, "published_time": stamp(now())})
        put("manifest.json", json.dumps({"slots": [{k: p[k] for k in ("slot", "file", "published_time")}
                                                   for p in published]}, indent=1).encode())
        with open(evidence / "published.jsonl", "a") as f:
            f.write(json.dumps(published[-1], sort_keys=True) + "\n")
        print(f"published {s['slot']} ({s['trips']} trips) at {published[-1]['published_time']}", flush=True)
    return evidence


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--to", choices=["local", "aws"], required=True)
    p.add_argument("--up-to", help="list only the files published by this time (ISO 8601, UTC)")
    p.add_argument("--live", action="store_true", help="publish each file at its time, from now on")
    p.add_argument("--slots", type=int, default=96, help="with --live: how many slots to publish")
    args = p.parse_args(argv)
    slots = day_slots()
    up_to = parse(args.up_to) if args.up_to else None
    if args.to == "local":
        folder = HERE / "feed"
        folder.mkdir(exist_ok=True)
        n = publish(lambda name, body: (folder / name).write_bytes(body), slots, up_to)
        print(f"{len(slots)} slot files and a manifest of {n} published slots in {folder.relative_to(ROOT)}/")
        return 0
    import boto3
    import lake

    session = boto3.Session(region_name=os.environ.get("AWS_REGION", "ca-central-1"))
    lake.up(session)
    bucket = lake.bucket_name(session)
    s3 = session.client("s3")
    if args.live:
        folder = live(s3, bucket, args.slots)
        print(f"done; publication times in {folder.relative_to(ROOT)}/published.jsonl")
        return 0
    n = publish(s3_put(s3, bucket), slots, up_to)
    print(f"{len(slots)} slot files and a manifest of {n} published slots in s3://{bucket}/{PREFIX}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

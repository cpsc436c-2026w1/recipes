"""Data latency of the feed poller: from a file's publication to the moment the stream accepted its last record.

    uv run python ext-source-poller/latency.py --published evidence/feed-<start>/published.jsonl
    uv run python ext-source-poller/latency.py --log poller.log --published ...     # log lines from a file

For each published slot, latency = `last_accepted` (the poller's log line for the slot, the time PutRecords
accepted the slot's last record) minus the time `feed.py --live` put the file (`published.jsonl`). A published slot
that was never loaded, or only failed, counts as over the target. The p95 is by the nearest rank:
the ceil(0.95 n)-th smallest, the 91st of 95. Target: under 20 minutes.

Without --log, the poller's lines are read from its log group `/aws/lambda/$PREFIX-feed-poller` (CloudWatch Logs,
FilterLogEvents for "last_accepted").
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import datetime
from pathlib import Path

TARGET_S = 20 * 60


def parse(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def nearest_rank(values: list[float], q: float = 0.95) -> float:
    ordered = sorted(values)
    return ordered[max(1, math.ceil(q * len(ordered))) - 1]


def latencies(published: list[dict], lines: list[dict]) -> dict[str, float]:
    """Seconds per published slot; infinite for one with no loaded or late line."""
    accepted = {}
    for line in lines:
        if line.get("slot_state") in ("loaded", "late") and line.get("last_accepted"):
            accepted.setdefault(line["slot"], parse(line["last_accepted"]))     # the first load counts
    return {p["slot"]: (accepted[p["slot"]] - parse(p["published_time"])).total_seconds()
            if p["slot"] in accepted else math.inf for p in published}


def log_lines(text: str) -> list[dict]:
    out = []
    for raw in text.splitlines():
        start = raw.find("{")
        if start < 0:
            continue
        try:
            row = json.loads(raw[start:])
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and "slot" in row:
            out.append(row)
    return out


def from_cloudwatch(logs, group: str) -> list[dict]:
    lines, kw = [], {"logGroupName": group, "filterPattern": '"last_accepted"'}
    while True:
        page = logs.filter_log_events(**kw)
        lines += log_lines("\n".join(e["message"] for e in page["events"]))
        if not page.get("nextToken"):
            return lines
        kw["nextToken"] = page["nextToken"]


def report(lat: dict[str, float], printed=print) -> bool:
    for slot, s in sorted(lat.items()):
        printed(f"{slot} {s / 60:.1f} min" if math.isfinite(s) else f"{slot} not loaded")
    p95 = nearest_rank(list(lat.values()))
    ok = p95 < TARGET_S
    printed(f"{len(lat)} latencies; p95 {p95 / 60:.1f} min (nearest rank {math.ceil(0.95 * len(lat))} of {len(lat)});"
            f" target under 20 min: {'met' if ok else 'NOT met'}")
    return ok


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--published", type=Path, required=True)
    p.add_argument("--log", type=Path, help="a file of the poller's log lines, instead of CloudWatch Logs")
    args = p.parse_args(argv)
    published = [json.loads(x) for x in args.published.read_text().splitlines() if x.strip()]
    if args.log:
        lines = log_lines(args.log.read_text())
    else:
        import boto3

        prefix = os.environ.get("PREFIX", "recipe")
        logs = boto3.client("logs", region_name=os.environ.get("AWS_REGION", "ca-central-1"))
        lines = from_cloudwatch(logs, f"/aws/lambda/{prefix}-feed-poller")
    return 0 if report(latencies(published, lines)) else 1


if __name__ == "__main__":
    sys.exit(main())

"""Data latency of one replay, at p95 by the nearest rank, from sent.jsonl and the first-seen times.

    uv run python experiments/latency.py evidence/<run-id>/

Reads evidence/<run-id>/sent.jsonl (the replayer's) and evidence/<run-id>/seen.jsonl, which your read step writes:
one line {"event_id", "seen_time"} per event, the first time the deployed table showed that event's effect. For each
event: seen_time minus the sent_time of its first delivery (joined on event_id, run id in front). An event never seen
counts as longer than every other. Prints the count, the median and the p95 in seconds.
"""
from __future__ import annotations

import json
import math
import sys
from datetime import datetime
from pathlib import Path


def parse(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def nearest_rank(values: list[float], q: float) -> float:
    """The value at rank ceil(q * n) of the sorted values (p95 of 2,511 events: the 2,386th smallest)."""
    ordered = sorted(values)
    return ordered[max(1, math.ceil(q * len(ordered))) - 1]


def latencies(folder: Path) -> list[float]:
    first_sent: dict[str, datetime] = {}
    for line in (folder / "sent.jsonl").read_text().splitlines():
        row = json.loads(line)
        t = parse(row["sent_time"])
        if row["result"] == "ok" and (row["event_id"] not in first_sent or t < first_sent[row["event_id"]]):
            first_sent[row["event_id"]] = t
    seen = {}
    seen_path = folder / "seen.jsonl"
    if seen_path.exists():
        seen = {r["event_id"]: parse(r["seen_time"]) for r in map(json.loads, seen_path.read_text().splitlines())}
    return [(seen[e] - t).total_seconds() if e in seen else math.inf for e, t in first_sent.items()]


def main(argv=None) -> int:
    folder = Path((argv or sys.argv[1:])[0])
    values = latencies(folder)
    print(f"{folder.name}: {len(values)} events, median {nearest_rank(values, 0.5):.1f} s, "
          f"p95 {nearest_rank(values, 0.95):.1f} s (nearest rank; an event never seen counts as infinite)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

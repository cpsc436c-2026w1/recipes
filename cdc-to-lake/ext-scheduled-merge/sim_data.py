"""The data sets of this extension, from simulated-runs' generator (sim/generate.py, copied unchanged).

    SIM01 rush-hour   simulated-runs' SIM01: 837 trips, 2,511 events as 2,515 deliveries (4 twice, 2 late)
    SIM02 late-day    SIM01's events moved to 2026-11-22, each delivered once, 1 s after it happened, except 12
                      requests of zone 161, delivered 30 hours later (2026-11-23, 15:00 to 16:00 UTC)
    SIM03 all-faults  simulated-runs' SIM04: SIM01 with 9 events never delivered and 2 corrected completions

Each is a list of deliveries (plain dicts, as in the generator's deliveries.jsonl files) and its truth: the answer
sim/truth.py computes from the events and faults under _truth/.
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Appended, not put first: a folder of this recipe never hides a module of the same name in another one
# (each extension has its own clean_up.py).
sys.path += [p for p in [str(ROOT / "sim"), str(ROOT / "scripts")] if p not in sys.path]
import generate  # noqa: E402
import truth  # noqa: E402
from rides.events import iso, parse  # noqa: E402

LATE_ZONE, LATE_REQUESTS, LATE_HOURS = 161, 12, 30          # ours: SIM02's held-back requests
NAMES = {"SIM01": "rush-hour", "SIM02": "late-day", "SIM03": "all-faults"}
SOURCE = {"SIM01": "SIM01", "SIM03": "SIM04"}                # this extension's id -> the generator's


def _generated(data_set: str, out: Path) -> Path:
    return generate.generate(data_set, out=out)


def _read(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def late_day(out: Path) -> tuple[list[dict], dict]:
    folder = _generated("SIM01", out)
    events = _read(folder / "_truth" / "events.jsonl")
    day = timedelta(days=1)
    moved = [{**e, "event_time": iso(parse(e["event_time"]) - day)} for e in events]
    held = [e["event_id"] for e in moved if e["event_type"] == "trip_requested"
            and e["pickup_zone_id"] == LATE_ZONE][:LATE_REQUESTS]
    deliveries = []
    for n, e in enumerate(sorted(moved, key=lambda e: (e["event_time"], e["event_id"])), 1):
        late = e["event_id"] in held
        received = parse(e["event_time"]) + (timedelta(hours=LATE_HOURS) if late else timedelta(seconds=1))
        deliveries.append({**e, "delivery_id": f"dl-{n:06d}", "received_time": iso(received)})
    deliveries.sort(key=lambda d: (d["received_time"], d["delivery_id"]))
    return deliveries, truth.compute(moved, [])


def load(data_set: str) -> tuple[list[dict], dict]:
    """(deliveries, truth) of SIM01, SIM02 or SIM03."""
    out = Path(tempfile.mkdtemp(prefix="merge-data-"))
    if data_set == "SIM02":
        return late_day(out)
    folder = _generated(SOURCE[data_set], out)
    return [json.loads(line) for line in generate.delivery_lines(folder)], truth.answer(folder)


SLOT = re.compile(r"/(\d{8}T\d{4}Z)-[^/]*\.jsonl$")


def slot_end(key: str, slot_min: int = 1) -> datetime | None:
    """The end of the slot an S3 target object holds (its key names the slot's start): when it lands in S3."""
    m = SLOT.search(key)
    if not m:
        return None
    return datetime.strptime(m.group(1), "%Y%m%dT%H%MZ").replace(tzinfo=timezone.utc) + timedelta(minutes=slot_min)

"""The answer of a data set, computed from sim/data/<id>/_truth/events.jsonl and _truth/faults.jsonl when a
scenario or a live run needs it; it is never stored, so it cannot drift from the events.

Each event_id is counted once, late events included, and the events of `never` (not delivered) are left out; for a
trip with a `changed` correction, its fields are those of its latest event of that kind (by event_time, then
event_id). A line that `broken` names (SIM02) is not an event and is left out. A Then compares the system's tables
with these numbers, never with the system's own counts.

Keys: events, trips, requested_per_zone {zone: trips}, requested_per_zone_day {UTC day: {zone: trips}}, accepted,
completed (trips with an accept, with a completion), fare_by_trip {trip_id: fare}. Add a key for each table your own
scenarios name, computed here from the same events.
"""
from __future__ import annotations

import json
from pathlib import Path

KINDS = ("twice", "late", "never", "changed")


def _latest(events: list[dict]) -> dict:
    return max(events, key=lambda e: (e["event_time"], e["event_id"]))


def read_faults(folder: Path) -> list[dict]:
    """_truth/faults.jsonl of a data set folder: one dict per injected fault."""
    text = (Path(folder) / "_truth" / "faults.jsonl").read_text()
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def by_kind(faults: list[dict]) -> dict[str, list[str]]:
    """The ids of each kind of fault: delivery ids for twice and late, event ids for never and changed."""
    key = {"twice": "delivery_id", "late": "delivery_id", "never": "event_id", "changed": "event_id"}
    return {kind: [f[key[kind]] for f in faults if f["fault"] == kind] for kind in KINDS}


def read_events(folder: Path, faults: list[dict] | None = None) -> list[dict]:
    """_truth/events.jsonl as plain dicts, without the lines a `broken` fault names."""
    faults = read_faults(folder) if faults is None else faults
    skip = {f["line"] for f in faults if f["fault"] == "broken"}
    lines = (Path(folder) / "_truth" / "events.jsonl").read_text().splitlines()
    return [json.loads(line) for n, line in enumerate(lines, 1) if line.strip() and n not in skip]


def answer(folder: Path) -> dict:
    """The answer of the data set in `folder`, from its _truth/ files."""
    faults = read_faults(folder)
    return compute(read_events(folder, faults), faults)


def compute(events: list[dict], faults: list[dict]) -> dict:
    """`events` as plain dicts (event_time as the fixed-width ISO string, which sorts as a time); `faults` as the
    lines of _truth/faults.jsonl."""
    never = {f["event_id"] for f in faults if f["fault"] == "never"}
    kept = {e["event_id"]: e for e in events if e["event_id"] not in never}
    by_trip: dict[str, dict[str, list[dict]]] = {}
    for e in kept.values():
        by_trip.setdefault(e["trip_id"], {}).setdefault(e["event_type"], []).append(e)
    per_zone: dict[str, int] = {}
    per_zone_day: dict[str, dict[str, int]] = {}
    fares: dict[str, float] = {}
    for trip, kinds in sorted(by_trip.items()):
        if "trip_requested" in kinds:
            req = _latest(kinds["trip_requested"])
            zone, day = str(req["pickup_zone_id"]), req["event_time"][:10]
            per_zone[zone] = per_zone.get(zone, 0) + 1
            per_zone_day.setdefault(day, {})[zone] = per_zone_day.get(day, {}).get(zone, 0) + 1
        if "trip_completed" in kinds:
            fares[trip] = _latest(kinds["trip_completed"])["fare"]
    return {
        "events": len(kept),
        "trips": len(by_trip),
        "requested_per_zone": dict(sorted(per_zone.items(), key=lambda kv: int(kv[0]))),
        "requested_per_zone_day": {d: dict(sorted(z.items(), key=lambda kv: int(kv[0])))
                                   for d, z in sorted(per_zone_day.items())},
        "accepted": sum(1 for k in by_trip.values() if "trip_accepted" in k),
        "completed": sum(1 for k in by_trip.values() if "trip_completed" in k),
        "fare_by_trip": fares,
    }

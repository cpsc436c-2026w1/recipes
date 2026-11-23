"""Seeded stand-in data sets for the source-poller and queue-ingest extensions, written in the simulator's layout.

    uv run python sim/stand_in.py                  # writes sim/data/<id>/ for every data set below
    uv run python sim/stand_in.py rush-hour        # one of them

The course simulator (simulated-runs' `sim/generate.py`) makes these data sets from DS01, the TLC High Volume FHV
trips. Its generator and DS01's sample are not in this folder, so this file makes data sets with the same counts,
the same faults and the same file layout from a seeded random source instead: the trips are **synthetic**, not
DS01's rows. Every line is built through the event models of `src/rides/events.py` and read back through them, so a
data set from `sim/generate.py` can replace one of these without a change to the code that reads it.

Data sets (the ids are the ones the extensions' scenarios name):

    rush-hour          SIM01 of ext-queue-ingest: 837 trips requested 09:00 to 10:00 UTC on 2026-11-23, 304, 191 and
                       342 in zones 48, 161 and 230; 2,511 events as 2,515 deliveries (4 requests twice, 2 requests
                       late by 6 to 10 minutes)
    refused-write      SIM02 of ext-queue-ingest: rush-hour's deliveries; the fault (the table refuses trip t-0412)
                       is put in by the scenario, not in the data
    completions-twice  SIM03 of ext-queue-ingest: rush-hour with 4 more completions twice: 2,519 deliveries
    feed-day           SIM01 of ext-source-poller: 19,074 completed trips on 2026-11-22 as 96 slot files of 15
                       minutes, each published 2 min after its slot ends; slot 09:15 never published, slot 10:30
                       published at 12:45

Files, in the simulator's layout (simulated-runs, README, Data): `trip-events/dt=<day>/hour=<hour>/deliveries.jsonl`
(one delivery line each, sorted keys, by day and hour of receipt), `_truth/events.jsonl` (each event once) and
`_truth/faults.jsonl` (one line per injected fault); the answer is computed from `_truth/` by `sim/truth.py`. The
feed day also has `_truth/slots.json` (each slot's trips count and publication time, or none).
"""
from __future__ import annotations

import json
import random
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from rides.events import (DELIVERY, EVENT, TripAccepted, TripCompleted, TripRequested,  # noqa: E402
                          as_delivery, read_jsonl, to_line)

DATA = ROOT / "sim" / "data"
SEED = 436
ZONES = {48: 304, 161: 191, 230: 342}          # trips per pickup zone in the rush hour (the spec's counts)
HOUR = datetime(2026, 11, 23, 9, 0, tzinfo=timezone.utc)
FEED_DAY = datetime(2026, 11, 22, tzinfo=timezone.utc)
FEED_TRIPS = 19_074
NEVER_SLOT = "2026-11-22T09:15Z"
LATE_SLOT, LATE_PUBLISHED = "2026-11-22T10:30Z", "2026-11-22T12:45:00Z"


def _completion(rng: random.Random, event_id: str, trip_id: str, driver: str, at: datetime, zone: int) -> TripCompleted:
    miles = round(rng.uniform(0.6, 9.5), 2)
    seconds = int(miles * rng.uniform(150, 330)) + 120
    fare = round(3.0 + miles * 2.9 + seconds / 60 * 0.55, 2)
    return TripCompleted(event_id=event_id, trip_id=trip_id, driver_id=driver, dropoff_zone_id=zone,
                         trip_miles=miles, trip_time_s=seconds, fare=fare, driver_pay=round(fare * 0.72, 2),
                         event_time=at)


def rush_hour_events(seed: int = SEED) -> list:
    """837 trips, each requested in the hour, accepted 3 to 15 s later and completed 5 minutes plus its trip time
    after the accept (the simulator's rule), in the order of their event times."""
    rng = random.Random(seed)
    zones = [z for z, n in ZONES.items() for _ in range(n)]
    rng.shuffle(zones)
    starts = sorted(rng.uniform(0, 3600) for _ in zones)
    events = []
    for i, (zone, start) in enumerate(zip(zones, starts), 1):
        trip, driver = f"t-{i:04d}", f"d-{rng.randrange(1, 121):04d}"
        requested = HOUR + timedelta(seconds=round(start, 3))
        accepted = requested + timedelta(seconds=rng.randint(3, 15))
        events.append(TripRequested(event_id=f"e-{i:04d}-r", trip_id=trip, pickup_zone_id=zone, event_time=requested))
        events.append(TripAccepted(event_id=f"e-{i:04d}-a", trip_id=trip, driver_id=driver, event_time=accepted))
        done = _completion(rng, f"e-{i:04d}-c", trip, driver, accepted, rng.choice(list(ZONES)))
        events.append(done.model_copy(update={"event_time": accepted + timedelta(seconds=300 + done.trip_time_s)}))
    return sorted(events, key=lambda e: (e.event_time, e.event_id))


def deliveries_of(events: list, twice: list[str], late: dict[str, int], seed: int = SEED) -> list:
    """One delivery per event, received 1 s after its event time; `twice` (event ids) get a second delivery with a
    new delivery_id a few seconds later; `late` (event id -> seconds) are received that much later."""
    rng = random.Random(seed + 1)
    out = []
    for e in events:
        received = e.event_time + timedelta(seconds=1 + late.get(e.event_id, 0))
        out.append(as_delivery(e, f"dl-{e.event_id}", received))
        if e.event_id in twice:
            out.append(as_delivery(e, f"dl-{e.event_id}-2", received + timedelta(seconds=rng.randint(2, 20))))
    return sorted(out, key=lambda d: (d.received_time, d.delivery_id))


def _write(data_set: str, events: list, deliveries: list, faults: list[dict], data_dir: Path = DATA, **more) -> Path:
    """The data set in the simulator's layout; what was there is removed first."""
    folder = data_dir / data_set
    shutil.rmtree(folder, ignore_errors=True)
    by_hour: dict[Path, list[str]] = {}
    for d in deliveries:
        r = d.received_time
        path = folder / "trip-events" / f"dt={r:%Y-%m-%d}" / f"hour={r:%H}" / "deliveries.jsonl"
        by_hour.setdefault(path, []).append(to_line(d))
    files = {**by_hour, folder / "_truth" / "events.jsonl": [to_line(e) for e in events],
             folder / "_truth" / "faults.jsonl": [json.dumps(f, sort_keys=True) for f in faults]}
    for path, lines in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(line + "\n" for line in lines))
    for name, value in more.items():
        (folder / "_truth" / f"{name}.json").write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    read_jsonl(folder / "_truth" / "events.jsonl", EVENT)                                            # read back
    for path in by_hour:
        read_jsonl(path, DELIVERY)
    return folder


def rush_hour(data_set: str = "rush-hour", data_dir: Path = DATA, extra_twice: int = 0) -> Path:
    events = rush_hour_events()
    rng = random.Random(SEED + 2)
    requests = [e.event_id for e in events if e.event_type == "trip_requested"]
    picked = rng.sample(requests, 6)
    twice, late = picked[:4], {eid: rng.randint(360, 600) for eid in picked[4:]}
    if extra_twice:
        twice += rng.sample([e.event_id for e in events if e.event_type == "trip_completed"], extra_twice)
    deliveries = deliveries_of(events, twice, late)
    by_event = {}
    for d in deliveries:
        by_event.setdefault(d.event_id, []).append(d.delivery_id)
    faults = ([{"fault": "twice", "delivery_id": by_event[e][1], "event_id": e} for e in twice]
              + [{"fault": "late", "delivery_id": f"dl-{e}", "event_id": e} for e in late])
    return _write(data_set, events, deliveries, faults, data_dir)


def slot_name(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%MZ")


def feed_day(data_set: str = "feed-day", data_dir: Path = DATA) -> Path:
    """96 slots of completed trips; a day's shape (quiet at night, busy at the rush hours), 19,074 in all."""
    import math

    rng = random.Random(SEED + 3)
    weights = [1.0 + 2.2 * math.exp(-((h - 8.5) / 1.6) ** 2) + 2.6 * math.exp(-((h - 18.0) / 2.2) ** 2)
               - 0.6 * math.exp(-((h - 4.0) / 2.0) ** 2) for h in (s / 4 for s in range(96))]
    raw = [w / sum(weights) * FEED_TRIPS for w in weights]
    counts = [int(x) for x in raw]
    for i in sorted(range(96), key=lambda i: raw[i] - counts[i], reverse=True)[:FEED_TRIPS - sum(counts)]:
        counts[i] += 1
    events, slots, n = [], [], 0
    for s, count in enumerate(counts):
        start = FEED_DAY + timedelta(minutes=15 * s)
        name = slot_name(start)
        times = sorted(rng.uniform(0, 899.999) for _ in range(count))
        for t in times:
            n += 1
            events.append(_completion(rng, f"fe-{n:05d}", f"ft-{n:05d}", f"d-{rng.randrange(1, 401):04d}",
                                      start + timedelta(seconds=round(t, 3)), rng.choice(list(ZONES))))
        published = None if name == NEVER_SLOT else LATE_PUBLISHED if name == LATE_SLOT else \
            (start + timedelta(minutes=17)).strftime("%Y-%m-%dT%H:%M:%SZ")
        slots.append({"slot": name, "file": f"trips-{start:%Y-%m-%dT%H%M}Z.jsonl", "trips": count,
                      "published_time": published})
    deliveries = deliveries_of(events, [], {})
    return _write(data_set, events, deliveries, [], data_dir, slots=slots)


MAKERS = {"rush-hour": rush_hour, "refused-write": lambda data_dir=DATA: rush_hour("refused-write", data_dir),
          "completions-twice": lambda data_dir=DATA: rush_hour("completions-twice", data_dir, extra_twice=4),
          "feed-day": feed_day}


def ensure(data_set: str, data_dir: Path = DATA) -> Path:
    """The data set's folder, written if it is not there yet."""
    folder = data_dir / data_set
    if not (folder / "_truth" / "events.jsonl").exists():
        MAKERS[data_set](data_dir=data_dir)
    return folder


if __name__ == "__main__":
    for name in sys.argv[1:] or MAKERS:
        folder = MAKERS[name](data_dir=DATA)
        print(f"{name}: {sum(1 for _ in open(folder / 'deliveries.jsonl'))} deliveries -> {folder.relative_to(ROOT)}")

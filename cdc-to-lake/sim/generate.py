"""Write a simulated data set, with its events as they happened and its injected faults, to sim/data/<id>/.

    uv run python sim/generate.py SIM01 --seed 436           # one data set
    uv run python sim/generate.py --all                      # SIM01 to SIM04
    uv run python sim/generate.py SIM01 --out ../other-data   # into another folder

Each data set folder holds:

    trip-events/dt=<day>/hour=<hour>/deliveries.jsonl
                          what the system receives: one line per delivery, an event's fields plus delivery_id and
                          received_time, in the order received; the folders are the day and hour of receipt, as
                          under raw/trip-events/ in the lake bucket
    _truth/events.jsonl   each event once, as it happened: the answer is computed from it (sim/truth.py)
    _truth/faults.jsonl   one line per injected fault: {"fault": "twice" | "late", "delivery_id", "event_id"},
                          {"fault": "never" | "changed", "event_id", "trip_id"}, and in SIM02
                          {"fault": "broken", "line", "field"}

Nothing under _truth/ is sent to the system: the replayer reads only the delivery files.

An injected fault is a problem the simulator adds to the data on purpose, and records, so the test knows the right
answer: `twice` a second delivery of the same event (new delivery_id); `late` a delivery received minutes after its
event_time; `never` an event that is not delivered; `changed` a correction, a second event of the same kind and trip
with a new event_id, a later event_time and a changed field.

Every line is built as a model and written with sorted keys, so the same seed gives the same bytes.
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import shutil
import sys
import tomllib
from datetime import datetime, timedelta
from pathlib import Path

from rides.events import (TripAccepted, TripCompleted, TripRequested, as_delivery, parse,
                          to_line)

sys.path.insert(0, str(Path(__file__).resolve().parent))
import truth  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CONFIG = tomllib.loads((ROOT / "sim" / "datasets.toml").read_text())
OUT = ROOT / "sim" / "data"
SOURCE = "trip-events"          # the folder of the delivery files, as raw/trip-events/ in the lake bucket


def read_seed_rows() -> list[dict]:
    """The DS01 sample, each request time moved to the simulated hour (minute and second kept)."""
    source_start = datetime.fromisoformat(CONFIG["source_start"])
    sim_start = parse(CONFIG["sim_start"])
    rows = []
    with open(ROOT / CONFIG["source"], newline="") as f:
        for n, row in enumerate(csv.DictReader(f), 1):
            requested = datetime.strptime(row["request_datetime"], "%Y-%m-%d %H:%M:%S")
            rows.append(dict(trip_id=f"t-{n:04d}", requested=sim_start + (requested - source_start),
                             pickup_zone_id=int(row["pickup_zone_id"]), dropoff_zone_id=int(row["dropoff_zone_id"]),
                             trip_miles=float(row["trip_miles"]), trip_time_s=int(row["trip_time_s"]),
                             fare=float(row["fare"]), driver_pay=float(row["driver_pay"])))
    return rows


def trip_events(rows: list[dict], rng: random.Random) -> list:
    """Three events per trip: the request; the accept 3 to 15 s later; the completion 5 minutes (to the pickup,
    our choice) plus the trip time after the accept. Event ids in the order the events happened."""
    raw = []
    for row in rows:
        driver = f"d-{rng.randint(1, 200):04d}"                     # DS01 has no drivers: ours
        accepted = row["requested"] + timedelta(seconds=rng.randint(3, 15))
        completed = accepted + timedelta(seconds=300 + row["trip_time_s"])
        raw.append(("trip_requested", row["requested"], row, driver))
        raw.append(("trip_accepted", accepted, row, driver))
        raw.append(("trip_completed", completed, row, driver))
    raw.sort(key=lambda r: (r[1], r[2]["trip_id"]))
    events = []
    for n, (kind, t, row, driver) in enumerate(raw, 1):
        common = dict(event_id=f"ev-{n:06d}", trip_id=row["trip_id"], event_time=t)
        if kind == "trip_requested":
            events.append(TripRequested(**common, pickup_zone_id=row["pickup_zone_id"]))
        elif kind == "trip_accepted":
            events.append(TripAccepted(**common, driver_id=driver))
        else:
            events.append(TripCompleted(**common, driver_id=driver, dropoff_zone_id=row["dropoff_zone_id"],
                                        trip_miles=row["trip_miles"], trip_time_s=row["trip_time_s"],
                                        fare=row["fare"], driver_pay=row["driver_pay"]))
    return events


def zone_of(events: list) -> dict[str, int]:
    return {e.trip_id: e.pickup_zone_id for e in events if e.event_type == "trip_requested"}


def stream(data_set: str, seed: int) -> tuple[list, list, dict]:
    """SIM01 and SIM04: (events, deliveries, faults). SIM04 draws SIM01's picks first, so both have the same
    twice and late requests; its never and changed trips are drawn from the trips those leave alone."""
    cfg, rng = CONFIG[data_set], random.Random(seed)
    events = trip_events(read_seed_rows(), rng)
    # (received_time, event, kind of delivery): kind "first", "twice" or "late"
    plan = {e.event_id: [e.event_time + timedelta(seconds=1), e, "first"] for e in events}
    extra: list[list] = []
    requests = [e for e in events if e.event_type == "trip_requested"]
    picked = rng.sample(requests, cfg.get("twice", 0) + cfg.get("late", 0))
    for e in picked[:cfg.get("twice", 0)]:
        extra.append([plan[e.event_id][0] + timedelta(seconds=rng.randint(2, 30)), e, "twice"])
    for e in picked[cfg.get("twice", 0):]:
        low, high = cfg["late_minutes"]
        plan[e.event_id] = [e.event_time + timedelta(seconds=rng.randint(low * 60, high * 60)), e, "late"]
    touched = {e.trip_id for e in picked}
    zones = zone_of(events)
    never_events: list[str] = []
    changed_events: list[str] = []
    if cfg.get("never_trips"):
        pool = sorted(t for t, z in zones.items() if z == cfg["never_zone"] and t not in touched)
        gone = set(rng.sample(pool, cfg["never_trips"]))
        touched |= gone
        never_events = [e.event_id for e in events if e.trip_id in gone]
    if cfg.get("changed_trips"):
        pool = sorted(t for t, z in zones.items() if z == cfg["changed_zone"] and t not in touched)
        fixed = sorted(rng.sample(pool, cfg["changed_trips"]))
        n = len(events)
        for trip in fixed:
            first = next(e for e in events if e.trip_id == trip and e.event_type == "trip_completed")
            n += 1
            correction = first.model_copy(update={
                "event_id": f"ev-{n:06d}", "event_time": first.event_time + timedelta(seconds=cfg["changed_after_s"]),
                "fare": round(first.fare + cfg["changed_fare_by"], 2)})
            events.append(correction)
            changed_events.append(correction.event_id)
            plan[correction.event_id] = [correction.event_time + timedelta(seconds=1), correction, "first"]
    pending = [v for k, v in plan.items() if k not in set(never_events)] + extra
    pending.sort(key=lambda p: (p[0], p[1].event_id, p[2] == "twice"))
    deliveries, faults = [], []
    for n, (received, event, kind) in enumerate(pending, 1):
        delivery = as_delivery(event, f"dl-{n:06d}", received)
        deliveries.append(delivery)
        if kind in ("twice", "late"):
            faults.append({"fault": kind, "delivery_id": delivery.delivery_id, "event_id": event.event_id})
    by_id = {e.event_id: e for e in events}
    faults.sort(key=lambda f: (f["fault"] == "late", f["delivery_id"]))
    faults += [{"fault": "never", "event_id": i, "trip_id": by_id[i].trip_id} for i in never_events]
    faults += [{"fault": "changed", "event_id": i, "trip_id": by_id[i].trip_id} for i in changed_events]
    events.sort(key=lambda e: (e.event_time, e.event_id))
    return events, deliveries, faults


def probe(data_set: str, seed: int) -> tuple[list, list, dict]:
    """SIM03: the first trips of one zone in SIM01's hour, each event delivered once, 1 s after it happened."""
    cfg = CONFIG[data_set]
    events = trip_events(read_seed_rows(), random.Random(seed))
    zones = zone_of(events)
    trips = [e.trip_id for e in events if e.event_type == "trip_requested" and zones[e.trip_id] == cfg["zone"]]
    keep = set(trips[:cfg["trips"]])
    events = [e for e in events if e.trip_id in keep]
    deliveries = [as_delivery(e, f"dl-{n:06d}", e.event_time + timedelta(seconds=1)) for n, e in enumerate(events, 1)]
    return events, deliveries, []


def broken(line: str, field: str) -> str:
    """One line of SIM02, broken on purpose in `field`."""
    data = json.loads(line)
    if field == "tip":                       # a field the model does not have
        data["tip"] = 2.5
    elif field == "event_time":              # a time without a time zone
        data["event_time"] = data["event_time"].removesuffix("Z").split(".")[0]
    else:                                    # a missing field
        data.pop(field)
    return json.dumps(data, sort_keys=True)


def bad_lines(data_set: str, seed: int) -> tuple[list[str], list, list[str], list[dict]]:
    """SIM02: the first events of SIM01, three of them broken in both files: (event lines, deliveries, delivery
    lines, faults). The line numbers in the faults are those of _truth/events.jsonl and of the delivery file."""
    cfg = CONFIG[data_set]
    events, deliveries, _ = stream("SIM01", seed)
    first = events[:cfg["first"]]
    marks = {int(k): v for k, v in cfg["broken"].items()}
    ev_lines = [broken(to_line(e), marks[n]) if n in marks else to_line(e) for n, e in enumerate(first, 1)]
    dl = [as_delivery(e, f"dl-{n:06d}", e.event_time + timedelta(seconds=1)) for n, e in enumerate(first, 1)]
    dl_lines = [broken(to_line(d), marks[n]) if n in marks else to_line(d) for n, d in enumerate(dl, 1)]
    faults = [{"fault": "broken", "line": n, "field": field} for n, field in sorted(marks.items())]
    return ev_lines, dl, dl_lines, faults


def delivery_path(folder: Path, received) -> Path:
    """Where a delivery received at `received` is written: <source>/dt=<day>/hour=<hour>/deliveries.jsonl."""
    return folder / SOURCE / f"dt={received:%Y-%m-%d}" / f"hour={received:%H}" / "deliveries.jsonl"


def delivery_files(folder: Path) -> list[Path]:
    """The delivery files of a data set folder, in the order received (day, then hour)."""
    return sorted(folder.glob("*/dt=*/hour=*/deliveries.jsonl"))


def delivery_lines(folder: Path) -> list[str]:
    """Every delivery line of a data set folder, in the order received."""
    return [line for path in delivery_files(folder) for line in path.read_text().splitlines() if line.strip()]


def generate(data_set: str, seed: int | None = None, out: Path | None = None) -> Path:
    """Write one data set to <out>/<id>/ (what was there is removed first) and return the folder."""
    if data_set not in CONFIG or not isinstance(CONFIG[data_set], dict):
        raise SystemExit(f"unknown data set {data_set}; sim/datasets.toml has "
                         f"{', '.join(k for k, v in CONFIG.items() if isinstance(v, dict))}")
    seed = CONFIG["seed"] if seed is None else seed
    folder = (out or OUT) / data_set
    shutil.rmtree(folder, ignore_errors=True)
    if data_set == "SIM02":
        ev_lines, deliveries, dl_lines, faults = bad_lines(data_set, seed)
    else:
        events, deliveries, faults = (probe if data_set == "SIM03" else stream)(data_set, seed)
        ev_lines, dl_lines = [to_line(e) for e in events], [to_line(d) for d in deliveries]
    files: dict[Path, list[str]] = {}
    for delivery, line in zip(deliveries, dl_lines):
        files.setdefault(delivery_path(folder, delivery.received_time), []).append(line)
    files[folder / "_truth" / "events.jsonl"] = ev_lines
    files[folder / "_truth" / "faults.jsonl"] = [json.dumps(f, sort_keys=True) for f in faults]
    for path, lines in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(line + "\n" for line in lines))
    return folder


def ensure(data_set: str, out: Path | None = None) -> Path:
    """The data set's folder, generated first when _truth/events.jsonl or the delivery files are missing."""
    folder = (out or OUT) / data_set
    if not (folder / "_truth" / "events.jsonl").exists() or not delivery_files(folder):
        generate(data_set, out=out)
    return folder


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("data_set", nargs="?", help="SIM01, SIM02, SIM03 or SIM04")
    p.add_argument("--all", action="store_true")
    p.add_argument("--seed", type=int)
    p.add_argument("--out", type=Path)
    args = p.parse_args(argv)
    names = [k for k, v in CONFIG.items() if isinstance(v, dict)] if args.all else [args.data_set]
    if not names[0]:
        p.error("name a data set or give --all")
    for name in names:
        folder = generate(name, args.seed, args.out)
        faults = truth.read_faults(folder)
        answer = truth.answer(folder)
        counts = ", ".join(f"{len(v)} {k}" for k, v in truth.by_kind(faults).items())
        events = len((folder / "_truth" / "events.jsonl").read_text().splitlines())
        print(f"{name} {CONFIG[name]['name']}: {events} events, {len(delivery_lines(folder))} deliveries in "
              f"{len(delivery_files(folder))} files; injected {counts}; truth {answer['events']} events, "
              f"{answer['trips']} trips, requested per zone {answer['requested_per_zone']} -> {folder}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

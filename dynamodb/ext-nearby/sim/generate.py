#!/usr/bin/env python3
"""Make ext-nearby's simulated data sets, the same every time (seed 436). README, Data.

    python3 sim/generate.py            writes sim/data/SIM01 to SIM04 (then run `nearby.py expect`)

SIM01  rush-hour lookups: one nearby request per trip request, 837 in zones 48, 161 and 230 (304, 191, 342) between
       09:00 and 10:00 UTC on 2026-11-23, each within 300 m of its zone's reference point; car type standard for 80%.
SIM02  drivers moving: 60 drivers, 20 per zone, a position every 30 s on a random walk of up to 15 m a step: 7,200
       positions, 72 of them (1%) delivered after the next one (fault `late`). positions.csv is what the system
       receives; _truth/events.jsonl holds each position as it happened and _truth/faults.jsonl one line per fault;
       nearby.sim_answer computes the counts (positions refused as older, cell changes) from them.
SIM03  edge of coverage: 5 points just outside the service area, 5 inside it more than 500 m from every SIM02 position.
SIM04  weather stations: 4 static points, coordinates approximate and chosen by hand.
"""
from __future__ import annotations

import csv
import json
import math
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import nearby  # noqa: E402

SEED = 436
START = datetime(2026, 11, 23, 9, 0, tzinfo=timezone.utc)
# reference point per pickup zone, chosen by hand inside each zone, approximate (README, Data)
ZONES = {48: (40.7620, -73.9930), 161: (40.7549, -73.9840), 230: (40.7590, -73.9845)}
REQUESTS_PER_ZONE = {48: 304, 161: 191, 230: 342}   # SIM01's trips per zone, as the sibling recipes' SIM01
DRIVERS_PER_ZONE = 20
STEPS = 120                                          # 60 minutes, one position every 30 s
LATE = 72
STATIONS = [("s-cpk", 40.7789, -73.9692), ("s-lga", 40.7794, -73.8803), ("s-jfk", 40.6386, -73.7622),
            ("s-ewr", 40.6825, -74.1694)]


def t(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def move(lat: float, lon: float, metres: float, bearing: float) -> tuple[float, float]:
    """A point `metres` away along `bearing` (radians), on a sphere; fine for steps of a few hundred metres."""
    dlat = metres * math.cos(bearing) / nearby.EARTH_M
    dlon = metres * math.sin(bearing) / (nearby.EARTH_M * math.cos(math.radians(lat)))
    return lat + math.degrees(dlat), lon + math.degrees(dlon)


def within(rng: random.Random, lat: float, lon: float, radius: float) -> tuple[float, float]:
    """A point drawn uniformly inside a disc of `radius` metres."""
    return move(lat, lon, radius * math.sqrt(rng.random()), rng.uniform(0, 2 * math.pi))


def write(path: Path, header: list[str], rows: list[list]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(header)
        w.writerows(rows)


def r6(x: float) -> float:
    return round(x, 6)


def sim01(rng: random.Random, data: Path) -> None:
    rows = []
    for zone, n in REQUESTS_PER_ZONE.items():
        for _ in range(n):
            lat, lon = within(rng, *ZONES[zone], 300)
            when = START + timedelta(seconds=rng.randrange(1, 3600))
            car = "standard" if rng.random() < 0.8 else "xl"
            rows.append([car, r6(lat), r6(lon), t(when), zone])
    rows.sort(key=lambda r: (r[3], r[4], r[1]))
    write(data / "SIM01" / "requests.csv", ["request_id", "car_type", "lat", "lon", "event_time", "pickup_zone_id"],
          [[f"q-{i:04d}"] + r for i, r in enumerate(rows, 1)])


def sim02(rng: random.Random, data: Path) -> list[tuple[float, float]]:
    drivers, positions, everywhere = [], [], []
    number = 0
    for zone, ref in ZONES.items():
        for k in range(DRIVERS_PER_ZONE):
            number += 1
            did = f"d-{number:03d}"
            car = "standard" if k < 16 else "xl"           # 80% standard
            lat, lon = within(rng, *ref, 300)
            offset = rng.randrange(0, 30)                     # each app sends on its own second
            first = START + timedelta(seconds=offset)
            drivers.append([did, car, r6(lat), r6(lon), t(first - timedelta(seconds=30)), zone])
            everywhere.append((lat, lon))
            for step in range(STEPS):
                lat, lon = move(lat, lon, rng.uniform(0, 15), rng.uniform(0, 2 * math.pi))
                everywhere.append((lat, lon))
                positions.append([did, car, r6(lat), r6(lon), first + timedelta(seconds=30 * step), step])
    # late: delivered after the driver's next position (never a driver's last position)
    candidates = [i for i, p in enumerate(positions) if p[5] < STEPS - 1]
    late = set(rng.sample(candidates, LATE))
    rows, faults = [], []
    for i, (did, car, lat, lon, when, step) in enumerate(positions):
        delivery = f"p-{i + 1:05d}"
        received = when + timedelta(seconds=30 + rng.randrange(1, 10)) if i in late else when
        if i in late:
            faults.append({"fault": "late", "delivery_id": delivery, "driver_id": did})
        rows.append([delivery, did, car, lat, lon, t(when), t(received)])
    write(data / "SIM02" / "drivers.csv", ["driver_id", "car_type", "lat", "lon", "start_time", "pickup_zone_id"],
          drivers)
    write(data / "SIM02" / "positions.csv",
          ["delivery_id", "driver_id", "car_type", "lat", "lon", "position_time", "received_time"], rows)
    truth = data / "SIM02" / "_truth"
    truth.mkdir(parents=True, exist_ok=True)
    happened = sorted(rows, key=lambda r: (r[5], r[1]))
    (truth / "events.jsonl").write_text("".join(json.dumps(
        {"delivery_id": r[0], "driver_id": r[1], "car_type": r[2], "lat": r[3], "lon": r[4], "position_time": r[5]},
        sort_keys=True) + "\n" for r in happened))
    (truth / "faults.jsonl").write_text("".join(json.dumps(f, sort_keys=True) + "\n" for f in faults))
    return everywhere


def sim03(data: Path, everywhere: list[tuple[float, float]]) -> None:
    (la0, la1), (lo0, lo1) = nearby.AREA["lat"], nearby.AREA["lon"]
    outside = [("e-out-1", la0 - 0.001, -73.9900), ("e-out-2", la1 + 0.001, -73.9400),
               ("e-out-3", 40.7500, lo0 - 0.001), ("e-out-4", 40.8000, lo1 + 0.001), ("e-out-5", la0 - 0.002, lo0 - 0.002)]
    inside = [("e-in-1", 40.8600, -73.9300), ("e-in-2", 40.7050, -74.0150), ("e-in-3", 40.8300, -73.9450),
              ("e-in-4", 40.7300, -73.9950), ("e-in-5", 40.7900, -73.9500)]
    for pid, lat, lon in inside:
        assert nearby.in_area(lat, lon)
        nearest = min(nearby.distance_m(lat, lon, a, b) for a, b in everywhere)
        assert nearest > nearby.RADIUS_M, (pid, nearest)
    for pid, lat, lon in outside:
        assert not nearby.in_area(lat, lon)
    write(data / "SIM03" / "points.csv", ["point_id", "where", "lat", "lon"],
          [[p, "outside", r6(a), r6(b)] for p, a, b in outside] + [[p, "inside", r6(a), r6(b)] for p, a, b in inside])


def sim04(data: Path) -> None:
    write(data / "SIM04" / "points.csv", ["point_id", "type", "lat", "lon"], [[p, "weather", a, b] for p, a, b in STATIONS])


def main(data: Path = nearby.DATA) -> None:
    rng = random.Random(SEED)
    sim01(rng, data)
    everywhere = sim02(rng, data)
    sim03(data, everywhere)
    sim04(data)
    print(f"wrote SIM01 to SIM04 under {data}")


if __name__ == "__main__":
    main()

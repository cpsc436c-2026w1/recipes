"""Writes data/trips_hour.csv from the DS01 hour of the course's Checkpoint 1 example, in the `trips` columns.

    git clone https://github.com/cpsc436c-2026w1/cp1-example /tmp/cp1   # used at commit 975b4c8
    uv run python ext-eval-matrix/data/make_trips_hour.py /tmp/cp1/sim/seed/ds01_sample.csv

The source holds the 837 DS01 trips requested in zones 48, 161 and 230 on 2024-03-14 between 08:00 and 09:00 (its
tools/extract_seed.sql). DS01 has no trip id, so `trip_id` is a hash of the source line and its position. This is
not the hash of make_trips_day.sql (that one hashes the DS01 columns), so the ids of the hour and of the day differ;
no test question uses them.
"""

import csv
import hashlib
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent / "trips_hour.csv"


def main(src: str) -> None:
    lines = Path(src).read_text().splitlines()
    with OUT.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["trip_id", "trip_state", "pickup_zone_id", "requested_time", "driver_id", "fare", "trip_miles"])
        for i, (raw, r) in enumerate(zip(lines[1:], csv.DictReader(lines))):
            trip_id = hashlib.sha256(f"{raw}|{i}".encode()).hexdigest()[:16]
            w.writerow([trip_id, "completed", r["pickup_zone_id"], r["request_datetime"], "", r["fare"],
                        r["trip_miles"]])
    print(f"wrote {OUT}: {len(lines) - 1} trips")


if __name__ == "__main__":
    main(sys.argv[1])

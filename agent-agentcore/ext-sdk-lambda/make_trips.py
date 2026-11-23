"""Build trips.parquet, the one day of trips the function ships (README, Data).

    uv run python ext-sdk-lambda/make_trips.py --from-tlc build/fhvhv_tripdata_2024-03.parquet --out build/trips.parquet
    uv run python ext-sdk-lambda/make_trips.py --synthetic --out build/trips-synthetic.parquet

--from-tlc  SIM01, the three-zone day: from DS01 (the TLC High Volume For-Hire Vehicle trip records of March 2024,
            the Uber trips, license HV0003), the trips requested in pickup zones 48, 161 and 230 on 2024-03-14.
            Download the month's file first (about 0.5 GB):
            https://d37ci6vzurychx.cloudfront.net/trip-data/fhvhv_tripdata_2024-03.parquet
            DS01 has no trip id, so trip_id is the MD5 of all the row's columns and its position in the file
            (`file_row_number`).
--synthetic A stand-in with the same columns and the same counts as SIM01 (4,345, 7,661 and 7,068 trips by zone,
            191 in zone 161 between 08:00 and 09:00), made from a fixed seed. The offline tests run on it; its
            fares, miles and the hours of the other trips are made up. Keep it out of build/trips.parquet, the
            file the package and the live eval read.

Both end by printing the counts the scenarios state and exit 1 when one differs.
"""
from __future__ import annotations

import argparse
import hashlib
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

import duckdb

DAY = "2024-03-14"
ZONES = {48: 4345, 161: 7661, 230: 7068}     # trips requested on DAY by pickup zone (SIM01)
ZONE_161_HOUR_8 = 191
TOTAL = sum(ZONES.values())                   # 19,074
COLUMNS = "trip_id VARCHAR, trip_state VARCHAR, pickup_zone_id INTEGER, requested_time TIMESTAMP, " \
          "driver_id VARCHAR, fare DOUBLE, trip_miles DOUBLE"


def from_tlc(source: Path, out: Path) -> None:
    con = duckdb.connect()
    con.execute(f"""
        COPY (
          SELECT md5(concat_ws('|', *COLUMNS(*))) AS trip_id,
                 'completed' AS trip_state,
                 CAST(PULocationID AS INTEGER) AS pickup_zone_id,
                 CAST(request_datetime AS TIMESTAMP) AS requested_time,
                 CAST(NULL AS VARCHAR) AS driver_id,
                 CAST(base_passenger_fare AS DOUBLE) AS fare,
                 CAST(trip_miles AS DOUBLE) AS trip_miles
          FROM read_parquet(?, file_row_number = true)
          WHERE hvfhs_license_num = 'HV0003' AND PULocationID IN (48, 161, 230)
            AND CAST(request_datetime AS DATE) = DATE '{DAY}'
          ORDER BY requested_time, trip_id
        ) TO '{out}' (FORMAT parquet)""", [str(source)])


def synthetic(out: Path, seed: int = 436) -> None:
    rng = random.Random(seed)
    start = datetime.fromisoformat(DAY)
    rows = []
    for zone, n in ZONES.items():
        for i in range(n):
            if zone == 161 and i < ZONE_161_HOUR_8:
                hour = 8
            else:
                hour = rng.choice([h for h in range(24) if not (zone == 161 and h == 8)])
            t = start + timedelta(hours=hour, seconds=rng.randrange(3600))
            fare = round(rng.uniform(8, 60), 2)
            miles = round(rng.uniform(0.3, 9), 2)
            rows.append((zone, t, fare, miles))
    rows.sort(key=lambda r: (r[1], r[0]))
    csv = out.with_suffix(".csv")
    csv.write_text("".join(f"{hashlib.md5(f'{z}|{t}|{f}|{m}|{pos}'.encode()).hexdigest()},completed,{z},{t},,{f},{m}\n"
                           for pos, (z, t, f, m) in enumerate(rows)))
    con = duckdb.connect()
    con.execute(f"CREATE TABLE trips ({COLUMNS})")
    con.execute("INSERT INTO trips SELECT * FROM read_csv(?, header = false, columns = {"
                "'a': 'VARCHAR', 'b': 'VARCHAR', 'c': 'INTEGER', 'd': 'TIMESTAMP', 'e': 'VARCHAR', 'f': 'DOUBLE', "
                "'g': 'DOUBLE'})", [str(csv)])
    con.execute(f"COPY trips TO '{out}' (FORMAT parquet)")
    csv.unlink()


def check(out: Path) -> list[str]:
    con = duckdb.connect()
    con.execute("CREATE TABLE trips AS SELECT * FROM read_parquet(?)", [str(out)])
    found = {"total": con.execute("SELECT COUNT(*) FROM trips").fetchone()[0],
             "zone 161, 08:00-09:00": con.execute(
                 "SELECT COUNT(*) FROM trips WHERE pickup_zone_id = 161 AND hour(requested_time) = 8").fetchone()[0]}
    found |= {f"zone {z}": c for z, c in con.execute(
        "SELECT pickup_zone_id, COUNT(*) FROM trips GROUP BY 1 ORDER BY 1").fetchall()}
    want = {"total": TOTAL, "zone 161, 08:00-09:00": ZONE_161_HOUR_8} | {f"zone {z}": n for z, n in ZONES.items()}
    problems = []
    for key, n in want.items():
        print(f"{key:<24} {found.get(key, 0):>6}  (SIM01: {n})")
        if found.get(key) != n:
            problems.append(key)
    return problems


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--from-tlc", type=Path, metavar="MONTH_FILE")
    src.add_argument("--synthetic", action="store_true")
    p.add_argument("--out", type=Path, required=True)
    a = p.parse_args(argv)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.unlink(missing_ok=True)
    from_tlc(a.from_tlc, a.out) if a.from_tlc else synthetic(a.out)
    problems = check(a.out)
    if problems:
        print("differs from SIM01: " + ", ".join(problems), file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

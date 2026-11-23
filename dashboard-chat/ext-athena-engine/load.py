"""DS01 (or SIM01) as the Parquet files of table trips, and DS02 as table zones, written locally.

  uv run python ext-athena-engine/load.py --data ds01     # download the TLC files, write build/athena/trips/...
  uv run python ext-athena-engine/load.py --data sim01    # only 2024-03-14 in zones 48, 161 and 230

DS01: the TLC High Volume For-Hire Vehicle records of March 2024, the Uber trips (base license HV0003) picked up in
Manhattan (the zones DS02 places there) in March 2024, 6,045,943 rows (measured in cp1-example; the month's file
holds exactly these pickups). One Parquet file per request day under trips/requested_day=<day>/, so Athena reads a
day's folder only (partitions, L11): 32 days, because 814 of the trips picked up on March 1 were requested on
February 29 (measured in the live run, 2026-11-23). deploy.py uploads the folder to chat/ in the lake bucket.

trip_id: DS01 has no trip id, so it is a hash of all the row's columns and the row's position in the file (the
values alone would give two identical rows one id).
"""

from __future__ import annotations

import argparse
import urllib.request
from pathlib import Path

import duckdb

HERE = Path(__file__).resolve().parent
BUILD = HERE.parent / "build" / "athena"
# The TLC's public files (not reachable from the build machine; checked in the live run).
TLC_TRIPS = "https://d37ci6vzurychx.cloudfront.net/trip-data/fhvhv_tripdata_2024-03.parquet"
TLC_ZONES = "https://d37ci6vzurychx.cloudfront.net/misc/taxi_zone_lookup.csv"
DS01_ROWS = 6_045_943
SIM01 = {"day": "2024-03-14", "zones": (48, 161, 230), "rows": 19_074}


def download(url: str, to: Path) -> Path:
    if not to.exists():
        to.parent.mkdir(parents=True, exist_ok=True)
        print(f"downloading {to.name} (TLC)")
        urllib.request.urlretrieve(url, to)
    return to


def write_tables(trips_file: Path, zones_csv: Path, out: Path, data: str = "ds01") -> dict:
    """Write out/trips/requested_day=<day>/*.parquet and out/zones/zones.parquet; return the row counts."""
    con = duckdb.connect()
    # Types as the DDL declares them (deploy.py): Athena refuses a Parquet BIGINT read as int.
    con.execute("CREATE TABLE zone_lookup AS SELECT CAST(LocationID AS INTEGER) AS zone_id, "
                "CAST(Borough AS VARCHAR) AS borough, CAST(Zone AS VARCHAR) AS zone_name, "
                f"CAST(service_zone AS VARCHAR) AS service_zone FROM read_csv_auto('{zones_csv}', header = true)")
    where = ""
    if data == "sim01":
        where = (f"AND CAST(request_datetime AS DATE) = DATE '{SIM01['day']}' "
                 f"AND PULocationID IN {tuple(SIM01['zones'])}")
    con.execute(f"""
        CREATE TABLE trips AS
        SELECT md5(concat_ws('|', r.hvfhs_license_num, r.dispatching_base_num, r.originating_base_num,
                             r.request_datetime, r.on_scene_datetime, r.pickup_datetime, r.dropoff_datetime,
                             r.PULocationID, r.DOLocationID, r.trip_miles, r.trip_time, r.base_passenger_fare,
                             r.driver_pay, r.file_row_number)) AS trip_id,
               'completed' AS trip_state,
               CAST(r.PULocationID AS INTEGER) AS pickup_zone_id,
               CAST(r.request_datetime AS TIMESTAMP) AS requested_time,
               CAST(NULL AS VARCHAR) AS driver_id,
               CAST(r.base_passenger_fare AS DOUBLE) AS fare,
               CAST(r.trip_miles AS DOUBLE) AS trip_miles,
               CAST(r.request_datetime AS DATE) AS requested_day
        FROM read_parquet('{trips_file}', file_row_number = true) r
        WHERE r.hvfhs_license_num = 'HV0003'
          AND r.PULocationID IN (SELECT zone_id FROM zone_lookup WHERE borough = 'Manhattan')
          AND r.pickup_datetime >= TIMESTAMP '2024-03-01' AND r.pickup_datetime < TIMESTAMP '2024-04-01'
          {where}""")
    (out / "trips").mkdir(parents=True, exist_ok=True)
    (out / "zones").mkdir(parents=True, exist_ok=True)
    con.execute(f"COPY trips TO '{out / 'trips'}' (FORMAT parquet, PARTITION_BY (requested_day), "
                f"OVERWRITE_OR_IGNORE true)")
    con.execute(f"COPY zone_lookup TO '{out / 'zones' / 'zones.parquet'}' (FORMAT parquet)")
    counts = {"trips": con.execute("SELECT COUNT(*) FROM trips").fetchone()[0],
              "days": con.execute("SELECT COUNT(DISTINCT requested_day) FROM trips").fetchone()[0],
              "zones": con.execute("SELECT COUNT(*) FROM zone_lookup").fetchone()[0]}
    con.close()
    return counts


def main(argv=None) -> dict:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data", choices=["ds01", "sim01"], default="sim01")
    p.add_argument("--out", type=Path, default=BUILD)
    args = p.parse_args(argv)
    trips = download(TLC_TRIPS, args.out.parent / "fhvhv_tripdata_2024-03.parquet")
    zones = download(TLC_ZONES, args.out.parent / "taxi_zone_lookup.csv")
    out = args.out / args.data
    counts = write_tables(trips, zones, out, args.data)
    want = DS01_ROWS if args.data == "ds01" else SIM01["rows"]
    shown = out.relative_to(HERE.parent) if out.is_relative_to(HERE.parent) else out
    print(f"{args.data}: {counts['trips']:,} trips over {counts['days']} request days, {counts['zones']} zones, in {shown}")
    if counts["trips"] != want:
        # A stop: DS01 is the Uber trips picked up in Manhattan in March (by pickup time, as the TLC's month file
        # is cut). Filtering by request time dropped the 814 trips requested on February 29 (live run, 2026-11-23).
        raise SystemExit(f"expected {want:,} trips for {args.data} (README, Data); got {counts['trips']:,}")
    return {"out": out, **counts}


if __name__ == "__main__":
    main()

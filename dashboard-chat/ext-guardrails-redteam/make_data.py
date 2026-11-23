"""The rides tables of the comment cases, as Parquet: the data sets SIM01 and SIM02.

  uv run python ext-guardrails-redteam/make_data.py          writes data/sim01/ and data/sim02/ under this folder

SIM01 "comments with instructions": the 837 trips of one simulated morning hour (2026-11-23 09:00 to 10:00 UTC; 304,
191 and 342 trips picked up in the zones 48, 161 and 230) and the 60 rider comments of comments.toml, 20 a zone, 10 of
which carry an instruction (data/sim01/injected.json lists them with their checks).
SIM02 "comments without instructions": the same trips and the 50 comments that carry none.

The trips are simulated with a fixed seed (same seed, same files: the stub replies and the tests rely on the numbers
staying put). Their columns are those of the course's trips table: trip_id, trip_state, pickup_zone_id,
requested_time, driver_id, fare. The comments are written for this recipe; trip records have no comments.
"""

from __future__ import annotations

import json
import random
import tomllib
from datetime import datetime, timedelta
from pathlib import Path

import duckdb

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "data"
COMMENTS_FILE = HERE / "comments.toml"
HOUR_START = datetime(2026, 11, 23, 9, 0)
TRIPS_PER_ZONE = {48: 304, 161: 191, 230: 342}  # 837 trips in all
SEED = 436
DATA_SETS = {"SIM01": "sim01", "SIM02": "sim02"}


def load_comments(path: Path = COMMENTS_FILE) -> list[dict]:
    """The comments of comments.toml, each with its time: 09:00 plus (its number - 1) minutes and 30 seconds."""
    comments = tomllib.loads(path.read_text())["comment"]
    for c in comments:
        c["comment_time"] = HOUR_START + timedelta(minutes=int(c["id"].split("-")[1]) - 1, seconds=30)
    return sorted(comments, key=lambda c: c["id"])


def injected(comments: list[dict]) -> list[dict]:
    return [{"comment_id": c["id"], "zone": c["zone"], "instruction": c["instruction"],
             "got_through_if": c["got_through_if"]} for c in comments if "instruction" in c]


def _trips() -> list[tuple]:
    rnd = random.Random(SEED)
    rows = []
    for zone, n in TRIPS_PER_ZONE.items():
        for _ in range(n):
            t = HOUR_START + timedelta(seconds=rnd.randrange(3600))
            state = rnd.choices(["completed", "accepted", "requested"], weights=[90, 6, 4])[0]
            driver = f"d-{rnd.randrange(1, 400):04d}" if state != "requested" else None
            fare = round(rnd.uniform(9.0, 48.0), 2) if state == "completed" else None
            rows.append([zone, t, state, driver, fare])
    rows.sort(key=lambda r: (r[1], r[0]))
    return [(f"t-{i:04d}", state, zone, t, driver, fare) for i, (zone, t, state, driver, fare) in enumerate(rows, 1)]


def make(data_dir: Path | None = None) -> dict[str, dict[str, Path]]:
    """Write both data sets; return {"SIM01": {"trips": path, "rider_comments": path}, "SIM02": {...}}."""
    data_dir = data_dir or DATA_DIR
    trips = _trips()
    comments = load_comments()
    by_zone: dict[int, list[str]] = {}
    for trip_id, _, zone, _, _, _ in trips:
        by_zone.setdefault(zone, []).append(trip_id)
    seen: dict[int, int] = {}
    rows = []
    for c in comments:  # each comment is on a trip of its zone: the zone's k-th trip for its k-th comment
        k = seen.get(c["zone"], 0)
        seen[c["zone"]] = k + 1
        rows.append((c["id"], by_zone[c["zone"]][k * 7], c["zone"], c["comment_time"], c["text"],
                     "instruction" in c))
    out = {}
    con = duckdb.connect()
    con.execute("CREATE TABLE trips (trip_id VARCHAR, trip_state VARCHAR, pickup_zone_id INTEGER, "
                "requested_time TIMESTAMP, driver_id VARCHAR, fare DOUBLE)")
    con.executemany("INSERT INTO trips VALUES (?, ?, ?, ?, ?, ?)", trips)
    con.execute("CREATE TABLE all_comments (comment_id VARCHAR, trip_id VARCHAR, pickup_zone_id INTEGER, "
                "comment_time TIMESTAMP, text VARCHAR, carries_instruction BOOLEAN)")
    con.executemany("INSERT INTO all_comments VALUES (?, ?, ?, ?, ?, ?)", rows)
    for name, folder in DATA_SETS.items():
        d = data_dir / folder
        d.mkdir(parents=True, exist_ok=True)
        paths = {"trips": d / "trips.parquet", "rider_comments": d / "rider_comments.parquet"}
        where = "" if name == "SIM01" else "WHERE NOT carries_instruction"
        con.execute(f"COPY trips TO '{paths['trips']}' (FORMAT parquet)")
        con.execute(f"COPY (SELECT comment_id, trip_id, pickup_zone_id, comment_time, text FROM all_comments {where} "
                    f"ORDER BY comment_id) TO '{paths['rider_comments']}' (FORMAT parquet)")
        out[name] = paths
    (data_dir / "sim01" / "injected.json").write_text(json.dumps(injected(comments), indent=1) + "\n")
    return out


def ensure(data_dir: Path | None = None) -> dict[str, dict[str, Path]]:
    """The two data sets, made when a file is missing. data_dir None: DATA_DIR, read when called (tests change it)."""
    data_dir = data_dir or DATA_DIR
    paths = {name: {"trips": data_dir / f / "trips.parquet", "rider_comments": data_dir / f / "rider_comments.parquet"}
             for name, f in DATA_SETS.items()}
    if all(p.exists() for ds in paths.values() for p in ds.values()) and (data_dir / "sim01" / "injected.json").exists():
        return paths
    return make(data_dir)


def injected_comments(data_dir: Path | None = None) -> dict[str, dict]:
    """comment_id -> the injected comment's entry of injected.json (its zone, instruction and check)."""
    data_dir = data_dir or DATA_DIR
    ensure(data_dir)
    return {c["comment_id"]: c for c in json.loads((data_dir / "sim01" / "injected.json").read_text())}


if __name__ == "__main__":
    for name, paths in make().items():
        con = duckdb.connect()
        n_trips = con.execute("SELECT COUNT(*) FROM read_parquet(?)", [str(paths["trips"])]).fetchone()[0]
        n_comments = con.execute("SELECT COUNT(*) FROM read_parquet(?)", [str(paths["rider_comments"])]).fetchone()[0]
        print(f"{name}: {n_trips} trips, {n_comments} comments in {paths['rider_comments'].parent}")

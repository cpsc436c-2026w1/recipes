"""The experiment's rules as plain functions: names, the growth rule, the spend bound, the statements, the run plan,
billed MB, what a REPORT line bills, and each cell's cost of 1,000 queries. No AWS call and no file here: the
backends (backends.py) and experiment.py call these, and the tests feed them the numbers of the scenarios.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import date, timedelta

MB = 2**20  # 1 MB = 2^20 bytes; 1 GB = 2^30; 1 TB = 2^40 (the course price sheet's units)
TB = 2**40
PROBE_MB = 10  # Athena's minimum billed per query: the size grows until the smallest query scans more
RUNS_PER_CELL = 10
SEED = 436
CTAS_DAYS = 100  # "Athena has a write limit of 100 partitions" for a CTAS or INSERT INTO

LAYOUTS = ("hive-flat", "hive-by-day", "iceberg-flat", "iceberg-by-day")
TABLE_STEM = {"hive-flat": "trips_hive_flat", "hive-by-day": "trips_hive_day", "iceberg-flat": "trips_ice_flat",
              "iceberg-by-day": "trips_ice_day"}
BY_DAY = ("hive-by-day", "iceberg-by-day")
QUERIES = ("one-day", "one-week", "whole-table")
DAY = date(2024, 3, 14)  # the one-day query's day
WEEK = (date(2024, 3, 11), date(2024, 3, 17))  # the one-week query's days, both included
COLUMNS = ["pickup_time", "pickup_zone_id", "dropoff_zone_id", "trip_miles", "trip_time_s", "fare", "driver_pay",
           "copy_no", "dt"]


def table(layout: str, copies: int) -> str:
    return f"{TABLE_STEM[layout]}_{copies}"


def copies_word(k: int) -> str:
    return f"{k} cop{'y' if k == 1 else 'ies'}"


# --- growth (R20, R30) ----------------------------------------------------------------------------------------------

@dataclass
class Growth:
    sizes: list[int]  # empty: no plan within MAX_USD
    note: str
    probes: dict[int, float]  # copies -> probe MB
    final_bound_usd: float


def plan_for(k: int) -> list[int]:
    """The three sizes the growth keeps at k copies: 1, k and 4k; 1, 2 and 4 when k is 1."""
    return [1, 2, 4] if k == 1 else [1, k, 4 * k]


MAX_COPIES = 256  # ours: a stop for a table so small that no plan reaches the spend limit (offline SIM01: 5 rows)
# ours: a doubling must grow the probe by at least half; below GROWTH_FLOOR_MB a file's own footer is most of what
# the query reads (offline SIM01), so the check starts there
MIN_GROWTH = 1.5
GROWTH_FLOOR_MB = 0.1


def grow(probe_mb, bound_usd, max_usd: float, max_copies: int = MAX_COPIES) -> Growth:
    """Double k from 1 until the probe (the one-day query's smaller Data scanned in the two by-day tables of k copies)
    passes 10 MB; before each doubling, stop if the bound of the plan the doubling leads to (1, 2k, 8k) is over
    max_usd. Then the bound of the kept plan, once more, decides whether any final table is built.

    probe_mb(k) builds the by-day tables of k copies and returns the probe; bound_usd(sizes) is the upper bound of the
    Athena spend of the plan's 360 runs, from the bytes of the tables built so far."""
    probes: dict[int, float] = {}
    k = 1
    while True:
        probes[k] = probe_mb(k)
        half = probes.get(k // 2) if k > 1 else None
        if half is not None and half >= GROWTH_FLOOR_MB and probes[k] < MIN_GROWTH * half:
            # Doubled rows that do not double the bytes are copies the files hold at little cost: a bigger table in
            # rows only, which tests nothing the experiment asks (the first live build)
            return Growth([], f"no plan: the probe did not grow with the copies ({half:g} MB at {copies_word(k // 2)}, "
                              f"{probes[k]:g} MB at {copies_word(k)})", probes, 0.0)
        if probes[k] > PROBE_MB:
            sizes, note = plan_for(k), f"10 MB reached at {copies_word(k)}"
            break
        if 8 * k > max_copies:
            sizes = plan_for(k)
            note = f"10 MB not reached: probe {probes[k]:g} MB at {copies_word(k)} (copies capped at {max_copies})"
            break
        if bound_usd([1, 2 * k, 8 * k]) > max_usd:
            sizes, note = plan_for(k), f"10 MB not reached: probe {probes[k]:g} MB at {copies_word(k)}"
            break
        k *= 2
    final = bound_usd(sizes)
    if final > max_usd:
        return Growth([], f"no plan within MAX_USD: upper bound {final:.2f} USD", probes, final)
    return Growth(sizes, note, probes, final)


@dataclass
class TableBytes:
    """Bytes of one copy of DS01 in a layout, measured from the tables built so far: all files, the largest day,
    and the largest seven days (by-day layouts; a flat layout's query may read every file)."""
    total: float
    day: float
    week: float


def query_upper_bytes(layout: str, query: str, copies: int, per_copy: TableBytes) -> float:
    """What a query may read, every column of every file it may read: a flat layout reads all of it; a by-day
    layout reads only its days' folders (partition pruning, L11)."""
    if layout not in BY_DAY or query == "whole-table":
        return per_copy.total * copies
    return (per_copy.day if query == "one-day" else per_copy.week) * copies


def plan_upper_bound_usd(sizes: list[int], per_copy: dict[str, TableBytes], usd_per_tb: float,
                         runs: int = RUNS_PER_CELL) -> float:
    total = 0.0
    for k in sizes:
        for layout in LAYOUTS:
            for q in QUERIES:
                total += runs * billed_mb(query_upper_bytes(layout, q, k, per_copy[layout])) * MB / TB * usd_per_tb
    return total


# --- statements (R10, R40) ------------------------------------------------------------------------------------------

def rotation_days(day_of_month: str = "day_of_month(dt)", copy_no: str = "c.copy_no") -> str:
    """The days a copy moves a row within March: copy n holds, on each day d, the trips of day d + n - 1 (wrapping
    after the 31st). Copy 1 is DS01 as it is; copies are distinct up to 31, and copy n + 31 repeats copy n. Ours,
    after the live builds of 2026-11-23: copies with the same rows on the same day took little more room than one
    (8 copies 251 MB, 16 copies 284 MB, the probe 1.02 then 1.12 MB, also without compression; the mechanism is
    unchecked); rotated, 8 copies take 506 MB and 16 copies 998 MB, the probe 1.2 then 2.5 MB."""
    day0 = f"({day_of_month} - 1)"
    return f"(((({day0} - ({copy_no} - 1)) % 31) + 31) % 31) - {day0}"


def copy_columns(dated: bool) -> str:
    """The SELECT list of a copy: DS01's columns and copy_no, dt last (Hive needs the partition column last).
    Dated copies move every date by 31 x (copy_no - 1) days; the others rotate the days of March (rotation_days)."""
    shift = "31 * (c.copy_no - 1)" if dated else rotation_days()
    return (f"date_add('day', {shift}, pickup_time) AS pickup_time, pickup_zone_id, dropoff_zone_id, "
            f"trip_miles, trip_time_s, fare, driver_pay, c.copy_no AS copy_no, "
            f"date_add('day', {shift}, dt) AS dt")


def with_clause(layout: str, location: str) -> str:
    if layout.startswith("hive"):
        parts = [f"format = 'PARQUET'", "write_compression = 'ZSTD'", f"external_location = '{location}'"]
        if layout == "hive-by-day":
            parts.append("partitioned_by = ARRAY['dt']")
    else:
        parts = ["table_type = 'ICEBERG'", "is_external = false", f"location = '{location}'", "format = 'PARQUET'",
                 "write_compression = 'ZSTD'"]
        if layout == "iceberg-by-day":
            parts.append("partitioning = ARRAY['day(pickup_time)']")
    return ", ".join(parts)


def day_chunks(first: date, last: date, size: int = CTAS_DAYS) -> list[tuple[date, date]]:
    out, d = [], first
    while d <= last:
        end = min(last, d + timedelta(days=size - 1))
        out.append((d, end))
        d = end + timedelta(days=1)
    return out


def build_statements(db: str, layout: str, copies: int, location: str, dated: bool = False,
                     first: date = date(2024, 3, 1), days_per_copy: int = 31) -> list[tuple[str, str, date, date]]:
    """(kind, sql, first day, last day) per statement: one CTAS, and for a by-day table of more than 100 days, the
    CTAS of its first 100 days and INSERT INTO statements of at most 100 days each (R40)."""
    days = days_per_copy * (copies if dated else 1)
    last = first + timedelta(days=days - 1)
    name = f"{db}.{table(layout, copies)}"
    # Measured in the live builds of 2026-11-23 (the mechanism is unchecked; it held without compression too): copies
    # of DS01's rows that a file holds next to each other take little more room than one copy. With DS01 first in
    # the CROSS JOIN the one-day probe stayed at 0.16-0.29 MB from 1 to 64 copies; with the copies first and the days
    # rotated (copy_columns) the by-day tables grew with the copies, but a flat table of 8 copies still scanned 1.4
    # times one copy for the whole table. So a flat table of more than 1 copy is written from the Hive by-day table
    # of the same size (its rows come day by day, each day mixing k days' trips), and build checks the growth of
    # every final table (scaling_problems).
    if layout not in BY_DAY and copies > 1:
        select = f"SELECT {', '.join(COLUMNS)} FROM {db}.{table('hive-by-day', copies)}"
    else:
        select = (f"SELECT {copy_columns(dated)} FROM UNNEST(sequence(1, {copies})) AS c(copy_no) "
                  f"CROSS JOIN {db}.ds01")
    chunks = day_chunks(first, last) if layout in BY_DAY else [(first, last)]
    out = []
    for i, (a, b) in enumerate(chunks):
        where = "" if len(chunks) == 1 else (f" WHERE date_add('day', 31 * (c.copy_no - 1), dt) BETWEEN DATE '{a}' "
                                             f"AND DATE '{b}'" if dated else f" WHERE dt BETWEEN DATE '{a}' AND DATE '{b}'")
        if i == 0:
            out.append(("CTAS", f"CREATE TABLE {name} WITH ({with_clause(layout, location)}) AS {select}{where}", a, b))
        else:
            out.append(("INSERT INTO", f"INSERT INTO {name} {select}{where}", a, b))
    return out


def query_sql(db: str, layout: str, copies: int, query: str) -> str:
    """Trips per pickup zone for one day, one week or the whole table; each layout gets its own best filter: the
    Hive by-day table filters on dt (its folders), the others on pickup_time (Iceberg's partitions follow it)."""
    t = f"{db}.{table(layout, copies)}"
    if query == "whole-table":
        where = ""
    else:
        a, b = (DAY, DAY) if query == "one-day" else WEEK
        if layout == "hive-by-day":
            where = (f" WHERE dt = DATE '{a}'" if a == b else f" WHERE dt BETWEEN DATE '{a}' AND DATE '{b}'")
        else:
            where = f" WHERE pickup_time >= TIMESTAMP '{a} 00:00:00' AND pickup_time < TIMESTAMP '{b + timedelta(days=1)} 00:00:00'"
    return f"SELECT pickup_zone_id, count(*) AS trips FROM {t}{where} GROUP BY 1"


def ds01_ctas(db: str, location: str) -> str:
    """DS01 from the course catalog: the Uber trips picked up in Manhattan in March 2024. pickup_datetime is
    timestamp(6) in the course's Iceberg table, and a Hive CTAS writes milliseconds only ("Incorrect timestamp
    precision for timestamp(6)", the first live build), so it is cast to timestamp(3)."""
    return f"""CREATE TABLE {db}.ds01 WITH (format = 'PARQUET', write_compression = 'ZSTD',
  external_location = '{location}ds01/') AS
SELECT CAST(r.pickup_datetime AS timestamp(3)) AS pickup_time, CAST(r.pulocationid AS INTEGER) AS pickup_zone_id,
       CAST(r.dolocationid AS INTEGER) AS dropoff_zone_id, r.trip_miles, CAST(r.trip_time AS BIGINT) AS trip_time_s,
       r.base_passenger_fare AS fare, r.driver_pay, CAST(r.pickup_datetime AS DATE) AS dt
FROM course.course_rides.rides r
WHERE r.pickup_datetime >= TIMESTAMP '2024-03-01 00:00:00' AND r.pickup_datetime < TIMESTAMP '2024-04-01 00:00:00'
  AND r.hvfhs_license_num = 'HV0003'
  AND r.pulocationid IN (SELECT locationid FROM course.course_rides.zones WHERE borough = 'Manhattan')"""


def build_order(layouts=LAYOUTS) -> list[str]:
    """The by-day layouts first: a flat table of more than 1 copy is written from the Hive by-day table."""
    return [lay for lay in layouts if lay in BY_DAY] + [lay for lay in layouts if lay not in BY_DAY]


SCALING_MIN = 0.75  # ours: a table of k copies must scan at least 0.75 x k times one copy for the whole table


def scaling_problems(scanned_mb: dict, sizes: list[int]) -> list[str]:
    """{(layout, copies): whole-table Data scanned MB} -> the tables that do not grow with their copies (from
    GROWTH_FLOOR_MB at 1 copy up: below it the files' own footers are most of what is read)."""
    out = []
    for (layout, k), mb in sorted(scanned_mb.items()):
        one = scanned_mb.get((layout, 1))
        if k > 1 and one is not None and one >= GROWTH_FLOOR_MB and mb < SCALING_MIN * k * one:
            out.append(f"{table(layout, k)} scans {mb:g} MB for the whole table, {mb / one:.1f} times 1 copy "
                       f"({one:g} MB), not about {k}")
    return out


# --- the run plan (R50) ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Run:
    engine: str
    layout: str
    copies: int
    query: str
    run_no: int

    @property
    def cell(self) -> tuple:
        return (self.engine, self.layout, self.copies, self.query)


def run_plan(sizes: list[int], seed: int = SEED, duckdb_engine: bool = True) -> list[Run]:
    runs = [Run("athena", lay, k, q, n) for k in sizes for lay in LAYOUTS for q in QUERIES
            for n in range(1, RUNS_PER_CELL + 1)]
    if duckdb_engine:
        runs += [Run("duckdb", "hive-by-day", k, q, n) for k in sizes for q in QUERIES
                 for n in range(1, RUNS_PER_CELL + 1)]
    random.Random(seed).shuffle(runs)
    return runs


REUSE_OFF = {"ResultReuseByAgeConfiguration": {"Enabled": False}}  # ResultReuseConfiguration of every run


# --- billed and excluded (R60, R70) ---------------------------------------------------------------------------------

def billed_mb(scanned_bytes: float, state: str = "SUCCEEDED", reused: bool = False) -> int:
    """Data scanned rounded up to whole MB, at least 10, for a query that ran (succeeded or cancelled); 0 for a
    failed query and for a reused result, which scanned nothing."""
    if reused or state == "FAILED":
        return 0
    return max(PROBE_MB, math.ceil(scanned_bytes / MB))


CUTOFF_REASON = "bytes scanned limit"


def exclusion(state: str, reused: bool, reason: str = "") -> str:
    """Why a run is left out of the times, or "" when it is kept."""
    if reused:
        return "result reused"
    if state == "CANCELLED" or CUTOFF_REASON in (reason or "").lower():
        return "scan cutoff"
    if state != "SUCCEEDED":
        return "failed"
    return ""


def spend_billed_mb(state: str, scanned_bytes: float, reused: bool, reason: str = "") -> int:
    """What a run adds to the spend: a cancelled run is billed for what it scanned, also when Athena reports the
    cutoff as FAILED."""
    if reused:
        return 0
    if state == "FAILED" and CUTOFF_REASON not in (reason or "").lower():
        return 0
    return billed_mb(scanned_bytes)


# --- Lambda REPORT lines and costs (R100, R120) ---------------------------------------------------------------------

def billed_gb_s(billed_duration_ms: float | None, memory_mb: int) -> float | None:
    if billed_duration_ms is None:  # no REPORT line was read for the call
        return None
    return round(billed_duration_ms / 1000 * memory_mb / 1024, 6)


def athena_cost_per_1000(median_billed_mb: float, usd_per_tb: float) -> float:
    return 1000 * median_billed_mb * MB / TB * usd_per_tb


def lambda_cost_per_1000(billed_ms: float, memory_mb: int, gets: float, prices: dict) -> float:
    per = (billed_gb_s(billed_ms, memory_mb) * prices["lambda_arm64_gb_s"] + prices["lambda_request"]
           + gets * prices["s3_get_per_1000"] / 1000)
    return 1000 * per


def results_differ(athena_rows: list, duckdb_rows: list) -> bool:
    """Rows compared keyed by zone: GROUP BY gives no order, so two right answers may come in different orders."""
    return dict(map(tuple, athena_rows)) != dict(map(tuple, duckdb_rows))

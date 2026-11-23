"""The SQL of the backfill, in Athena's dialect and in DuckDB's, built by the same functions.

Athena runs the first on AWS. Offline, DuckDB runs the second over local files, and the Athena text is kept so a
test can read what would have been sent. The two dialects differ in two places only, the hash and the parsing of a
time written as text; everything else (try_cast, concat_ws, row_number, CAST(... AS date)) is written once.

A source is described by a `Source` (from `history.toml`, or the built-in one of the course catalog):
the columns and their types, the time column, how to read a time written as text, an id column if the source has
a stable one, and which source column fills each column of `trips_history`.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

HISTORY_COLUMNS = ["trip_id", "pickup_zone_id", "dropoff_zone_id", "requested_time", "trip_miles", "trip_time_s",
                   "fare", "driver_pay", "origin", "run_id", "requested_day"]
TYPES = {"pickup_zone_id": "bigint", "dropoff_zone_id": "bigint", "requested_time": "timestamp",
         "trip_miles": "double", "trip_time_s": "bigint", "fare": "double", "driver_pay": "double"}
NULL_TEXT = r"\N"          # a null column in the text that is hashed
ATHENA, DUCKDB = "athena", "duckdb"

# The course catalog's table course.course_rides.rides, as Athena's GetTableMetadata listed it on 2026-11-22.
COURSE_COLUMNS = {
    "hvfhs_license_num": "string", "dispatching_base_num": "string", "originating_base_num": "string",
    "request_datetime": "timestamp", "on_scene_datetime": "timestamp", "pickup_datetime": "timestamp",
    "dropoff_datetime": "timestamp", "pulocationid": "bigint", "dolocationid": "bigint", "trip_miles": "double",
    "trip_time": "bigint", "base_passenger_fare": "double", "tolls": "double", "bcf": "double",
    "sales_tax": "double", "congestion_surcharge": "double", "airport_fee": "double", "tips": "double",
    "driver_pay": "double", "shared_request_flag": "string", "shared_match_flag": "string",
    "access_a_ride_flag": "string", "wav_request_flag": "string", "wav_match_flag": "string",
    "cbd_congestion_fee": "double"}
RIDES_MAP = {"pickup_zone_id": "pulocationid", "dropoff_zone_id": "dolocationid", "requested_time": "request_datetime",
             "trip_miles": "trip_miles", "trip_time_s": "trip_time", "fare": "base_passenger_fare",
             "driver_pay": "driver_pay"}
ZONES = (48, 161, 230)     # the three zones of the rides example


@dataclass
class Source:
    kind: str                                 # "course" or "files"
    columns: dict[str, str]                   # source column -> string | bigint | double | timestamp
    map: dict[str, str]                       # trips_history column -> source column
    time_column: str
    time_format: str = "%Y-%m-%d %H:%M:%S"    # how a time written as text reads (strftime codes)
    id_column: str = ""
    format: str = "parquet"                   # files: csv | jsonl | parquet
    where: dict[str, str] = field(default_factory=dict)   # dialect -> extra filter
    pickup_column: str = ""                   # course: the partition column, for pruning
    text: bool = False                        # columns are declared string in the external table

    def declared(self, column: str) -> str:
        return "string" if self.text else self.columns[column]


def course_source() -> Source:
    zones = ", ".join(str(z) for z in ZONES)
    rides_filter = f"hvfhs_license_num = 'HV0003' AND pulocationid IN ({zones})"
    return Source("course", dict(COURSE_COLUMNS), dict(RIDES_MAP), "request_datetime",
                  where={ATHENA: rides_filter, DUCKDB: rides_filter}, pickup_column="pickup_datetime")


def files_source(path: Path) -> Source:
    """The team's own files, described in history.toml."""
    t = tomllib.loads(Path(path).read_text())
    fmt = t["format"]
    if fmt not in ("csv", "jsonl", "parquet"):
        raise SystemExit(f"history.toml: format must be csv, jsonl or parquet, not {fmt!r}")
    columns = {k.lower(): v for k, v in t["columns"].items()}
    bad = {k: v for k, v in columns.items() if v not in ("string", "bigint", "double", "timestamp")}
    if bad:
        raise SystemExit(f"history.toml: column types are string, bigint, double or timestamp: {bad}")
    mapping = {k: v.lower() for k, v in t["map"].items()}
    missing = [c for c in TYPES if c not in mapping]
    if missing:
        raise SystemExit(f"history.toml: [map] needs a source column for {', '.join(missing)}")
    where = t.get("where", "")
    return Source("files", columns, mapping, t["time_column"].lower(), t.get("time_format", "%Y-%m-%d %H:%M:%S"),
                  t.get("id_column", "").lower(), fmt, {ATHENA: where, DUCKDB: where} if where else {},
                  text=fmt in ("csv", "jsonl"))


def athena_time_format(fmt: str) -> str:
    """strftime codes to the MySQL-style codes of Athena's date_parse (%M minutes is %i there, %S seconds is %s)."""
    return fmt.replace("%M", "%i").replace("%S", "%s")


def hash_text(src: Source) -> str:
    """Every source column as text, a null as \\N, joined by |: the text whose hash is a row's id."""
    parts = [f"coalesce(CAST({c} AS varchar), '{NULL_TEXT}')" for c in src.columns]
    return "concat_ws('|', " + ", ".join(parts) + ")"


def hash16(text: str, dialect: str) -> str:
    """The first 16 hex digits (lower case) of SHA-256 of a text."""
    if dialect == ATHENA:
        return f"lower(substr(to_hex(sha256(to_utf8({text}))), 1, 16))"
    return f"substr(sha256({text}), 1, 16)"


def typed(src: Source, column: str, dialect: str) -> str:
    """A source column as the type it has in trips_history; text columns are cast here, a bad value to null."""
    s, target = src.map[column], TYPES[column]
    declared = src.declared(s)
    if declared == target == "timestamp" and dialect == ATHENA:
        # A CTAS or INSERT INTO a Hive Parquet table writes milliseconds only, and refuses the course table's
        # timestamp(6) ("Incorrect timestamp precision for timestamp(6)", the live run of 2026-11-23): Athena's
        # `timestamp` is timestamp(3)
        return f"CAST({s} AS timestamp)"
    if declared == target:
        return s
    if declared == "string":
        return time_of(src, s, dialect) if target == "timestamp" else f"try_cast({s} AS {target})"
    return f"CAST({s} AS {target})"


def time_of(src: Source, column: str, dialect: str) -> str:
    if src.declared(column) != "string":
        return column
    if dialect == ATHENA:
        return f"try(date_parse({column}, '{athena_time_format(src.time_format)}'))"
    return f"try_strptime({column}, '{src.time_format}')"


def _ts(d: date | datetime) -> str:
    return f"TIMESTAMP '{d:%Y-%m-%d %H:%M:%S}'"


def source_filter(src: Source, days: list[date], dialect: str) -> str:
    """The rows of these days: requested on one of them, and (course) picked up from the first day to two days after
    the last, so Athena reads only the month partitions it needs (the table is partitioned by pickup month). A trip
    picked up after midnight of the last day is kept; one picked up before the first day's midnight is not: DS01 has
    6,538 pickups before their request time, none of them before 2024-03-01 in the rides example (measured), and the
    count check of R120 shows any such trip as a difference."""
    t = time_of(src, src.time_column, dialect)
    first, last = min(days), max(days)
    parts = [f"{t} >= {_ts(first)}", f"{t} < {_ts(last + timedelta(days=1))}"]
    if len(days) != (last - first).days + 1:
        parts.append(f"CAST({t} AS date) IN ({', '.join(f'DATE {chr(39)}{d}{chr(39)}' for d in sorted(days))})")
    if src.pickup_column:
        parts += [f"{src.pickup_column} >= {_ts(first)}", f"{src.pickup_column} < {_ts(last + timedelta(days=2))}"]
    if src.where.get(dialect):
        parts.append(f"({src.where[dialect]})")
    return " AND ".join(parts)


def history_select(src: Source, relation: str, days: list[date], run_id: str, dialect: str) -> str:
    """The rows of trips_history for these days, with requested_day last (a CTAS partition column comes last)."""
    if src.id_column:
        trip_id, inner_id = "CAST(src_id AS varchar)", f"{src.id_column} AS src_id"
    else:
        trip_id = "concat(h, '-', CAST(row_number() OVER (PARTITION BY h) AS varchar))"
        inner_id = f"{hash16(hash_text(src), dialect)} AS h"
    cols = ",\n         ".join(f"{typed(src, c, dialect)} AS {c}" for c in TYPES)
    return (f"SELECT {trip_id} AS trip_id, {', '.join(TYPES)},\n"
            f"       'backfill' AS origin, '{run_id}' AS run_id, requested_day\n"
            f"FROM (SELECT {inner_id},\n         {cols},\n"
            f"         CAST({time_of(src, src.time_column, dialect)} AS date) AS requested_day\n"
            f"      FROM {relation}\n      WHERE {source_filter(src, days, dialect)}) AS s")


def ctas(src: Source, relation: str, db: str, location: str, days: list[date], run_id: str) -> str:
    return (f"CREATE TABLE {db}.trips_history\n"
            f"WITH (format = 'PARQUET', write_compression = 'ZSTD', external_location = '{location}',\n"
            f"      partitioned_by = ARRAY['requested_day'])\n"
            f"AS {history_select(src, relation, days, run_id, ATHENA)}")


def insert_into(src: Source, relation: str, db: str, days: list[date], run_id: str) -> str:
    return f"INSERT INTO {db}.trips_history\n{history_select(src, relation, days, run_id, ATHENA)}"


def source_counts(src: Source, relation: str, days: list[date], dialect: str) -> str:
    t = time_of(src, src.time_column, dialect)
    return (f"SELECT CAST(CAST({t} AS date) AS varchar) AS day, count(*) AS n FROM {relation}\n"
            f"WHERE {source_filter(src, days, dialect)} GROUP BY 1")


def history_counts(relation: str, days: list[date]) -> str:
    return (f"SELECT CAST(requested_day AS varchar) AS day, count(*) AS n FROM {relation}\n"
            f"WHERE requested_day BETWEEN DATE '{min(days)}' AND DATE '{max(days)}' GROUP BY 1")


def external_table(src: Source, db: str, location: str) -> str:
    """CREATE EXTERNAL TABLE over the team's files: every column a string for CSV and JSON Lines (cast in the CTAS),
    the declared types for Parquet."""
    cols = ",\n  ".join(f"`{c}` {src.declared(c)}" for c in src.columns)
    head = f"CREATE EXTERNAL TABLE IF NOT EXISTS {db}.history_raw (\n  {cols}\n)\n"
    if src.format == "csv":
        return (head + "ROW FORMAT SERDE 'org.apache.hadoop.hive.serde2.OpenCSVSerde'\n"
                "WITH SERDEPROPERTIES ('separatorChar' = ',', 'quoteChar' = '\"')\n"
                f"LOCATION '{location}'\nTBLPROPERTIES ('skip.header.line.count' = '1')")
    if src.format == "jsonl":
        return head + f"ROW FORMAT SERDE 'org.openx.data.jsonserde.JsonSerDe'\nLOCATION '{location}'"
    return head + f"STORED AS PARQUET\nLOCATION '{location}'"


def view(db: str, cutover: datetime, history: str = "", live: str = "") -> str:
    """trips_all: the history before the cutover time, then the live table from it, one row per trip."""
    history, live = history or f"{db}.trips_history", live or f"{db}.trips"
    return (f"CREATE OR REPLACE VIEW {db}.trips_all AS\n"
            f"SELECT trip_id, 'completed' AS trip_state, pickup_zone_id, dropoff_zone_id, requested_time, trip_miles,\n"
            f"       trip_time_s, fare, driver_pay, origin, run_id, requested_day\n"
            f"FROM {history} WHERE requested_time < {_ts(cutover)}\n"
            f"UNION ALL\n"
            f"SELECT trip_id, trip_state, CAST(pickup_zone_id AS bigint), CAST(NULL AS bigint),\n"
            f"       CAST(requested_time AS timestamp), CAST(NULL AS double), CAST(NULL AS bigint),\n"
            f"       CAST(fare AS double), CAST(NULL AS double), 'live', CAST(NULL AS varchar),\n"
            f"       CAST(requested_time AS date)\n"
            f"FROM {live} WHERE requested_time >= {_ts(cutover)}")


def left_out(relation: str, cutover: datetime) -> str:
    return f"SELECT count(*) AS n FROM {relation} WHERE requested_time >= {_ts(cutover)}"


def months(days: list[date]) -> list[list[date]]:
    """The days grouped by calendar month, oldest first: one statement each, at most 31 partitions."""
    out: dict[tuple[int, int], list[date]] = {}
    for d in sorted(days):
        out.setdefault((d.year, d.month), []).append(d)
    return list(out.values())

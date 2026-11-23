"""The rides case: table `trips`, its test questions with groups, the SQL step's prompt, and its offline stub.

The table holds DS01 rows (TLC High Volume For-Hire Vehicle trips, Uber, picked up in Manhattan, March 2024) in the
course's `trips` columns. Offline it is `data/trips_hour.csv`: the 837 trips requested in zones 48, 161 and 230 on
2024-03-14 between 08:00 and 09:00, the DS01 rows of the course's public Checkpoint 1 example
(cpsc436c-2026w1/cp1-example, `sim/seed/ds01_sample.csv`). `data/make_trips_day.sql` builds the whole day (SIM01,
19,074 trips) from DS01 as `data/trips.parquet`; when that file is there, `--trips` can name it. The reference
values are queries, so they are computed on whichever table is loaded.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path

import duckdb
import yaml

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
HOUR = DATA / "trips_hour.csv"
TEST = DATA / "test.yml"

SCHEMA = """Table trips: one row per trip requested in Manhattan pickup zones 48, 161 and 230 on 2024-03-14.
  trip_id         VARCHAR     the trip's id
  trip_state      VARCHAR     requested, accepted or completed (every trip here is completed)
  pickup_zone_id  INTEGER     the TLC taxi zone of the pickup: 48, 161 or 230
  requested_time  TIMESTAMP   when the rider requested the trip, New York local time
  driver_id       VARCHAR     the driver (empty: the source has no drivers)
  fare            DOUBLE      the base fare the rider paid, in dollars
  trip_miles      DOUBLE      the distance of the trip, in miles"""

SEED_INSTRUCTIONS = ("Answer the question about the day's trips with one DuckDB SQL query over the table given. "
                     "The app runs the query and shows its single value to the user. If the table cannot answer "
                     "the question, reply with CANNOT_ANSWER and nothing else.")


def system_prompt(instructions: str) -> str:
    return f"{instructions}\n\nReply with the SQL only.\n\n{SCHEMA}"


class Trips:
    """One DuckDB connection over the trips table; `run_sql` runs a reply the way the app would."""

    def __init__(self, path: Path = HOUR):
        self.path = Path(path)
        self.con = duckdb.connect()
        if self.path.suffix == ".parquet":
            self.con.execute(f"CREATE TABLE trips AS SELECT * FROM read_parquet('{self.path}')")
        else:
            self.con.execute(
                f"CREATE TABLE trips AS SELECT * FROM read_csv('{self.path}', header = true, types = {{"
                "'trip_id': 'VARCHAR', 'trip_state': 'VARCHAR', 'pickup_zone_id': 'INTEGER', "
                "'requested_time': 'TIMESTAMP', 'driver_id': 'VARCHAR', 'fare': 'DOUBLE', 'trip_miles': 'DOUBLE'})")
        self.rows = self.con.execute("SELECT COUNT(*) FROM trips").fetchone()[0]
        self._lock = threading.Lock()

    def run_sql(self, sql: str) -> tuple[float | None, str | None]:
        """One read-only SELECT that returns one number: (value, None), or (None, the error)."""
        sql = extract_sql(sql)
        with self._lock:
            try:
                statements = self.con.extract_statements(sql)
            except duckdb.Error as e:
                return None, f"DuckDB could not parse it: {str(e).splitlines()[0]}"
            if len(statements) != 1:
                return None, f"{len(statements)} statements; the app runs exactly one"
            if statements[0].type != duckdb.StatementType.SELECT:
                return None, f"a {statements[0].type.name} statement; the app runs only SELECT"
            try:
                rows = self.con.execute(sql).fetchall()
            except duckdb.Error as e:
                return None, f"DuckDB refused it: {str(e).splitlines()[0]}"
        if len(rows) != 1 or len(rows[0]) != 1:
            return None, f"{len(rows)} rows; the app needs one row, one column"
        value = rows[0][0]
        try:
            return float(value), None
        except (TypeError, ValueError):
            return None, f"the result {value!r} is not a number"


def extract_sql(reply: str) -> str:
    """The query in a reply: the inside of a ``` block if there is one, else the reply."""
    m = re.search(r"```(?:sql)?\s*\n?(.*?)```", reply, flags=re.S | re.I)
    return (m.group(1) if m else reply).strip()


def load_cases(path: Path, trips: Trips) -> list[dict]:
    """The test cases, with each answerable case's reference value computed on `trips`. Groups are not checked
    here: the matrix checks them first, before anything else (R80)."""
    cases = yaml.safe_load(Path(path).read_text())
    for c in cases:
        c.setdefault("answerable", "sql" in c)
        if c["answerable"]:
            value, error = trips.run_sql(c["sql"])
            if error:
                raise ValueError(f"reference SQL of {c['id']} does not run: {error}")
            c["reference"] = value
    return cases


# ---- the offline stub of the SQL step ---------------------------------------------------------------------------
# Each habit, and the words in the system prompt that stop it. The seed prompt contains none of them; the
# hand-written example prompt data/after-example.txt contains all three. A stand-in, not a model: it shows that
# the groups, the counts and the comparison work.
STOPPED_BY = {
    "inclusive-end": "before the end time",
    "first-zone": "both zones",
    "guess": "has no column for",
}
HABITS = {
    "stub-small": {"inclusive-end", "first-zone", "guess"},
    "stub-large": {"inclusive-end"},
}
REPLIES = {r["question"]: r for r in yaml.safe_load((DATA / "stub_rides.yml").read_text())}


def stub_reply(stub_model: str, system: str, question: str) -> str:
    """The reply the stub SQL step writes for `question` under the instructions in `system`."""
    norm = re.sub(r"\s+", " ", system.lower())
    habits = {h for h in HABITS[stub_model] if STOPPED_BY[h] not in norm}
    r = REPLIES.get(question.strip())
    if r is None:
        return "CANNOT_ANSWER"
    for habit in ("inclusive-end", "first-zone", "guess"):
        if habit in habits and habit in r:
            return r[habit]
    return r["reply"]

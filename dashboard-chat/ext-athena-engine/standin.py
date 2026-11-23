"""Offline stand-ins: a clock that only moves when something waits, a stand-in for Athena that runs the same SELECT
in DuckDB over the same Parquet files, and SIM01's counts as a small synthetic table.

Nothing here is used in a live run. The stand-in speaks Athena's wire shape (GetQueryResults gives every value as
text, the header row first, and the column types in ResultSetMetadata), so query_fn.py's casting is the same code
offline and live.
"""

from __future__ import annotations

import itertools
import threading
from pathlib import Path

import duckdb

# SIM01 "three-zone day": the trips requested in zones 48, 161 and 230 on 2024-03-14 (README, "Data": measured
# from DS01 by request day). Offline it is a synthetic table with these counts, not DS01's rows.
SIM01_DAY = "2024-03-14"
SIM01_COUNTS = {48: 4_345, 161: 7_661, 230: 7_068}
ZONES = [(48, "Manhattan", "Clinton East", "Yellow Zone"), (161, "Manhattan", "Midtown Center", "Yellow Zone"),
         (230, "Manhattan", "Times Sq/Theatre District", "Yellow Zone"), (100, "Manhattan", "Garment District",
                                                                           "Yellow Zone")]


class VirtualClock:
    """Seconds that pass only when someone sleeps; now() starts at `start`. Thread-safe for the tests' use."""

    real = False

    def __init__(self, start: float = 1_710_400_000.0):
        self.t = start
        self.t0 = start
        self._lock = threading.Lock()

    def now(self) -> float:
        with self._lock:
            return self.t

    def sleep(self, s: float) -> None:
        with self._lock:
            self.t += max(0.0, s)

    def elapsed(self) -> float:
        return self.now() - self.t0


def make_sim01(folder: Path) -> dict[str, Path]:
    """Write SIM01 (synthetic, with SIM01's counts) as trips/requested_day=2024-03-14/part-0.parquet and zones."""
    trips_dir = folder / "trips" / f"requested_day={SIM01_DAY}"
    trips_dir.mkdir(parents=True, exist_ok=True)
    (folder / "zones").mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    parts = " UNION ALL ".join(
        f"SELECT {z} AS z, i FROM range({n}) t(i)" for z, n in SIM01_COUNTS.items())
    con.execute(f"""
        COPY (SELECT md5(z || '-' || i) AS trip_id, 'completed' AS trip_state, CAST(z AS INTEGER) AS pickup_zone_id,
                     TIMESTAMP '{SIM01_DAY} 00:00:00' + to_seconds(CAST(hash(z * 100003 + i) % 86400 AS BIGINT))
                         AS requested_time,
                     CAST(NULL AS VARCHAR) AS driver_id, ROUND(5 + (hash(i * 7 + z) % 4000) / 100.0, 2) AS fare,
                     ROUND((hash(i * 11 + z) % 900) / 100.0, 2) AS trip_miles
              FROM ({parts}))
        TO '{trips_dir / "part-0.parquet"}' (FORMAT parquet)""")
    zones = ", ".join(f"({z}, '{b}', '{n}', '{s}')" for z, b, n, s in ZONES)
    con.execute(f"""COPY (SELECT * FROM (VALUES {zones}) t(zone_id, borough, zone_name, service_zone))
                    TO '{folder / "zones" / "zones.parquet"}' (FORMAT parquet)""")
    con.close()
    return {"trips": folder / "trips", "zones": folder / "zones"}


_TYPES = {"BIGINT": "bigint", "INTEGER": "integer", "SMALLINT": "smallint", "TINYINT": "tinyint", "HUGEINT": "bigint",
          "DOUBLE": "double", "FLOAT": "float", "VARCHAR": "varchar", "DATE": "date", "TIMESTAMP": "timestamp",
          "BOOLEAN": "boolean"}


def athena_type(duck_type: str) -> str:
    t = str(duck_type).upper()
    if t.startswith("DECIMAL"):
        return t.lower()
    return _TYPES.get(t, "varchar")


class ClientError(Exception):
    """The shape of botocore's ClientError that query_fn.py reads (response["Error"]["Code"])."""

    def __init__(self, code: str, message: str):
        super().__init__(f"An error occurred ({code}): {message}")
        self.response = {"Error": {"Code": code, "Message": message}}


class StandInAthena:
    """The Athena calls query_fn.py makes, answered by DuckDB over local Parquet files.

    takes_s: virtual seconds a query runs before it ends; None keeps every query running (R210's "never finishes").
    scanned_bytes: the Data scanned a finished query reports; None reports the size of the files it read.
    cutoff_bytes: the workgroup's per-query cutoff; a query that would scan more is cancelled with Athena's reason.
    allowed_workgroups: the workgroups the role may start queries in; another one is AccessDeniedException.
    """

    def __init__(self, tables: dict[str, Path], clock, *, takes_s: float | None = 0.5, scanned_bytes: int | None = None,
                 cutoff_bytes: int = 104_857_600, allowed_workgroups: tuple[str, ...] | None = None):
        self.clock = clock
        self.takes_s = takes_s
        self.scanned_bytes = scanned_bytes
        self.cutoff_bytes = cutoff_bytes
        self.allowed_workgroups = allowed_workgroups
        self.con = duckdb.connect(":memory:")
        self.file_bytes = {}
        for name, folder in tables.items():
            files = sorted(Path(folder).rglob("*.parquet"))
            self.file_bytes[name] = sum(f.stat().st_size for f in files)
            hive = "true" if any("=" in f.parent.name for f in files) else "false"
            self.con.execute(f"CREATE VIEW {name} AS SELECT * FROM read_parquet('{Path(folder)}/**/*.parquet', "
                             f"hive_partitioning = {hive})")
        self.queries: dict[str, dict] = {}
        self.started: list[str] = []  # SQL of every StartQueryExecution, in order
        self.stops: list[str] = []  # ids of every StopQueryExecution
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    # --- the API query_fn.py calls ---
    def start_query_execution(self, QueryString, WorkGroup, QueryExecutionContext=None, **_):
        if self.allowed_workgroups is not None and WorkGroup not in self.allowed_workgroups:
            raise ClientError("AccessDeniedException", f"not authorized to perform athena:StartQueryExecution on "
                                                       f"workgroup/{WorkGroup}")
        with self._lock:
            qid = f"q-{next(self._ids):04d}"
            self.started.append(QueryString)
        q = {"sql": QueryString, "workgroup": WorkGroup, "database": (QueryExecutionContext or {}).get("Database", ""),
             "start": self.clock.now(), "state": "RUNNING", "reason": "", "scanned": 0, "result": None}
        try:
            cur = self.con.cursor()
            cur.execute(QueryString)
            q["result"] = ([d[0] for d in cur.description], [athena_type(d[1]) for d in cur.description],
                           cur.fetchall())
            q["would_scan"] = self.scanned_bytes if self.scanned_bytes is not None else self._read_bytes(QueryString)
        except duckdb.Error as e:
            q["error"] = str(e).splitlines()[0]
        self.queries[qid] = q
        return {"QueryExecutionId": qid}

    def get_query_execution(self, QueryExecutionId):
        q = self.queries[QueryExecutionId]
        if q["state"] == "RUNNING":
            self._advance(q)
        return {"QueryExecution": {
            "QueryExecutionId": QueryExecutionId, "Query": q["sql"], "WorkGroup": q["workgroup"],
            "QueryExecutionContext": {"Database": q["database"]},
            "Status": {"State": q["state"], "StateChangeReason": q["reason"]},
            "Statistics": {"DataScannedInBytes": q["scanned"]}}}

    def stop_query_execution(self, QueryExecutionId):
        self.stops.append(QueryExecutionId)
        q = self.queries[QueryExecutionId]
        if q["state"] == "RUNNING":
            q["state"], q["reason"] = "CANCELLED", "Query cancelled by user"
            q["scanned"] = min(q.get("would_scan") or 0, self.cutoff_bytes)
        return {}

    def get_query_results(self, QueryExecutionId, MaxResults=1000, NextToken=None):
        columns, types, rows = self.queries[QueryExecutionId]["result"]
        page = [{"Data": [{"VarCharValue": c} for c in columns]}]
        for r in rows[: MaxResults - 1]:
            page.append({"Data": [{} if v is None else {"VarCharValue": _text(v)} for v in r]})
        out = {"ResultSet": {"Rows": page, "ResultSetMetadata": {"ColumnInfo": [
            {"Name": c, "Type": t, "Label": c} for c, t in zip(columns, types)]}}}
        if len(rows) > MaxResults - 1:
            out["NextToken"] = "more"
        return out

    # --- the stand-in's own ---
    def _advance(self, q: dict) -> None:
        if "error" in q:
            q["state"], q["reason"] = "FAILED", q["error"]
            return
        if (q.get("would_scan") or 0) > self.cutoff_bytes:
            q["state"], q["reason"] = "FAILED", "Query cancelled! : Bytes scanned limit was exceeded"
            q["scanned"] = self.cutoff_bytes
            return
        if self.takes_s is not None and self.clock.now() >= q["start"] + self.takes_s:
            q["state"], q["scanned"] = "SUCCEEDED", q["would_scan"]

    def _read_bytes(self, sql: str) -> int:
        low = sql.lower()
        return sum(n for name, n in self.file_bytes.items() if name in low)


def _text(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


class ScriptedModel:
    """A stand-in model: its first reply holds the scripted tool calls, each later reply is "The answer is {value}."
    with the first value of the last tool result's first row (so the number in the reply comes from the rows, as in a
    real run). takes_s: seconds each reply takes on the clock; a reply that would take longer than the request's
    timeout (request_timeout_s, set by chat_athena.DeadlineModel) waits that long and raises TimeoutError, as httpx
    does. Every call is recorded with its start time and timeout."""

    def __init__(self, sqls: list[str], clock, *, takes_s: float = 0.0, model: str = "qwen3.6-35b-a3b",
                 replies: dict[str, list[str]] | None = None):
        self.sqls = list(sqls)
        self.replies = replies  # question -> SQL list, for several questions (the eval)
        self.clock = clock
        self.takes_s = takes_s
        self.model = model
        self.request_timeout_s = None
        self.calls: list[dict] = []

    def chat(self, messages, tools=None):
        from main.model import ModelReply, ToolCall

        start = self.clock.now()
        self.calls.append({"start": start, "timeout_s": self.request_timeout_s})
        wait = self.takes_s
        if self.request_timeout_s is not None and wait > self.request_timeout_s:
            self.clock.sleep(self.request_timeout_s)
            self.calls[-1]["cut_off_at"] = self.clock.now()
            raise TimeoutError("timed out")
        self.clock.sleep(wait)
        tokens_in = sum(len(str(m.get("content") or "")) for m in messages) // 4
        last = messages[-1]
        if last["role"] == "tool":
            import json

            result = json.loads(last["content"])
            if "error" in result:
                text = "I could not run a query for that question."
            elif result.get("rows"):
                text = f"The answer is {result['rows'][0][-1]}."
            else:
                text = "The query returned no rows."
            return ModelReply(content=text, tokens_in=tokens_in, tokens_out=len(text) // 4, model=self.model)
        question = next(m["content"] for m in reversed(messages) if m["role"] == "user")
        sqls = self.replies.get(question, []) if self.replies is not None else self.sqls
        if not sqls:
            return ModelReply(content="I can only answer questions about the trips.", tokens_in=tokens_in,
                              tokens_out=10, model=self.model)
        n = len(self.calls)
        calls = [ToolCall(id=f"call-{n}-{i}", name="run_sql", arguments={"sql": s}) for i, s in enumerate(sqls)]
        return ModelReply(content=None, tool_calls=calls, tokens_in=tokens_in,
                          tokens_out=sum(len(s) for s in sqls) // 4, model=self.model)


class Offline:
    """Everything an offline question needs: SIM01 in a folder, the stand-in for Athena, the usage table in moto
    (the caller starts moto), the function run in-process, and the engine."""

    def __init__(self, folder: Path, clock, *, names=None, limits=None, tables: dict | None = None, **athena_kw):
        import boto3

        from athena_engine import AthenaEngine, LocalInvoker
        from settings import Limits, Names

        self.names = names or Names()
        self.limits = limits or Limits()
        self.clock = clock
        self.tables = tables or make_sim01(folder)
        self.athena = StandInAthena(self.tables, clock, **athena_kw)
        self.ddb = boto3.client("dynamodb", region_name="ca-central-1")
        create_usage_table(self.ddb, self.names.usage_table)
        env = {"WORKGROUP": self.names.workgroup, "DATABASE": self.names.database,
               "USAGE_TABLE": self.names.usage_table}
        self.invoker = LocalInvoker(self.athena, self.ddb, env, clock, self.limits)
        self.engine = AthenaEngine(self.invoker, limits=self.limits)

    def billed_today_bytes(self) -> int:
        from datetime import datetime, timezone

        day = datetime.fromtimestamp(self.clock.now(), timezone.utc).strftime("%Y-%m-%d")
        item = self.ddb.get_item(TableName=self.names.usage_table, Key={"usage_date": {"S": day}}).get("Item", {})
        return int(item.get("billed_bytes", {}).get("N", "0"))

    def set_billed_today_mb(self, mb: int) -> None:
        from datetime import datetime, timezone

        from settings import MB

        day = datetime.fromtimestamp(self.clock.now(), timezone.utc).strftime("%Y-%m-%d")
        self.ddb.put_item(TableName=self.names.usage_table,
                          Item={"usage_date": {"S": day}, "billed_bytes": {"N": str(mb * MB)}})


def create_usage_table(ddb, name: str) -> str:
    """The daily cap's table: on demand, key usage_date, items deleted by TTL 3 days after their day (deploy.py makes
    the same table live, with the course tags)."""
    try:
        ddb.describe_table(TableName=name)
        return "borrowed"
    except ddb.exceptions.ResourceNotFoundException:
        pass
    ddb.create_table(TableName=name, KeySchema=[{"AttributeName": "usage_date", "KeyType": "HASH"}],
                     AttributeDefinitions=[{"AttributeName": "usage_date", "AttributeType": "S"}],
                     BillingMode="PAY_PER_REQUEST")
    ddb.get_waiter("table_exists").wait(TableName=name)
    return "created"

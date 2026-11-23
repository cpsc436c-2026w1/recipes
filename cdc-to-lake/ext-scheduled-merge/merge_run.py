"""The hourly merge: raw JSON Lines objects into `trips`, then the counts per period rebuilt, with a run log.

This file is the Lambda function `<PREFIX>-merge-run` (handler `merge_run.handler`); `merge.py` deploys it and
the scenarios call `run()` with DuckDB in place of Athena. One run:

  1. takes the lease (item `lock` of job `merge` in `<PREFIX>-runs`), and skips if the last run that is still
     `running` has a query that Athena still runs (R102)
  2. lists the open arrival days `raw/trip-events/dt=<day>/` and takes the keys no merge manifest holds
     (`lake/merge-state/dt=<day>.txt`), at least SETTLE_S old, oldest first, at most MAX_OBJECTS (R81)
  3. checks every line against the delivery model; an object with a bad line is copied to `quarantine/` and left
     out (R103); events older than K + 1 days are counted as `old_events` (R91)
  4. MERGE INTO trips (sql/merge_trips.sql over sql/trips_source.sql) (R81, R82)
  5. MERGE INTO zone_counts for the periods of the last K + 1 days (sql/rebuild_counts.sql) (R90)
  6. only after both succeed: the manifests, then item `last-finished` (R100, R101)

The run's time is passed in, and so is the place the objects are listed from (`Store`): a test plays the hours of a
day and sets each object's LastModified, which S3 does not let a caller choose.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from rides.events import DELIVERY, LineError, iso, parse, validate_line

HERE = Path(__file__).resolve().parent
SQL = HERE / "sql"
JOB = "merge"
RAW = "raw/trip-events/"
MANIFESTS = "lake/merge-state/"
QUARANTINE = "quarantine/trip-events/"
PERIODS = ("day", "week", "month")
EVENT_COLUMNS = ("event_type", "event_id", "trip_id", "pickup_zone_id", "driver_id", "fare", "event_time")


def sql_text(name: str) -> str:
    """sql/<name>.sql without its comment lines."""
    lines = (SQL / f"{name}.sql").read_text().splitlines()
    return "\n".join(line for line in lines if not line.lstrip().startswith("--")).strip()


@dataclass(frozen=True)
class Config:
    """From the environment (the function's, or the shell's): every number has a default."""
    prefix: str = field(default_factory=lambda: os.environ.get("PREFIX") or "recipe")
    bucket: str = field(default_factory=lambda: os.environ.get("BUCKET", ""))
    k_days: int = field(default_factory=lambda: int(os.environ.get("K_DAYS", "3")))        # ours: 48 h late, Q010
    period: str = field(default_factory=lambda: os.environ.get("PERIOD", "day"))
    max_objects: int = field(default_factory=lambda: int(os.environ.get("MAX_OBJECTS", "500")))
    merge_from: str = field(default_factory=lambda: os.environ.get("MERGE_FROM", ""))
    settle_s: int = field(default_factory=lambda: int(os.environ.get("SETTLE_S", "300")))   # ours: 5 min
    lock_s: int = field(default_factory=lambda: int(os.environ.get("LOCK_S", "900")))       # ours: 15 min > timeout

    def __post_init__(self):
        if self.period not in PERIODS:
            raise ValueError(f"PERIOD must be one of {PERIODS}, not {self.period!r}")

    @property
    def database(self) -> str:
        return f"{self.prefix.replace('-', '_')}_lake"

    @property
    def run_log(self) -> str:
        return f"{self.prefix}-runs"

    @property
    def workgroup(self) -> str:
        return f"{self.prefix}-wg"


# ---- where the objects are --------------------------------------------------------------------------------------

@dataclass
class Obj:
    key: str
    day: str                      # the arrival day of its folder
    last_modified: datetime
    lines: list[dict] = field(default_factory=list)    # the validated deliveries, as plain dicts
    bad: list[str] = field(default_factory=list)       # "line 3, field trip_id: ..."


class Store:
    """The lake bucket. `stamps` overrides an object's LastModified (offline only: S3 sets it, not the caller)."""

    def __init__(self, s3, bucket: str):
        self.s3, self.bucket = s3, bucket
        self.stamps: dict[str, datetime] = {}

    def list(self, prefix: str) -> list[tuple[str, datetime]]:
        out = []
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix):
            for o in page.get("Contents", []):
                out.append((o["Key"], self.stamps.get(o["Key"], o["LastModified"])))
        return out

    def days(self) -> list[str]:
        """The arrival days that have a folder under raw/trip-events/."""
        found = []
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=RAW, Delimiter="/"):
            found += [p["Prefix"][len(RAW) + 3:-1] for p in page.get("CommonPrefixes", [])]
        return sorted(d for d in found if len(d) == 10)

    def read(self, key: str) -> str | None:
        try:
            return self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read().decode()
        except self.s3.exceptions.NoSuchKey:
            return None

    def put(self, key: str, text: str) -> None:
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=text.encode())

    def copy(self, key: str, to: str) -> None:
        self.s3.copy_object(Bucket=self.bucket, Key=to, CopySource={"Bucket": self.bucket, "Key": key})

    def path(self, key: str) -> str:
        return f"s3://{self.bucket}/{key}"


def manifest(store: Store, day: str) -> dict[str, str]:
    """key -> merged | quarantined, from lake/merge-state/dt=<day>.txt (one line per key, a tab between)."""
    text = store.read(f"{MANIFESTS}dt={day}.txt") or ""
    return dict(line.split("\t", 1) for line in text.splitlines() if "\t" in line)


# ---- the two engines --------------------------------------------------------------------------------------------

class StepFailed(RuntimeError):
    def __init__(self, step: str, query_id: str, text: str):
        self.step, self.query_id = step, query_id
        super().__init__(text)


class AthenaEngine:
    """Athena in the enforced workgroup `<PREFIX>-wg` (its results location is the workgroup's)."""

    name = "athena"

    def __init__(self, athena, cfg: Config, sleep=time.sleep, wait_s: int = 480):
        self.athena, self.cfg, self.sleep, self.wait_s = athena, cfg, sleep, wait_s
        self.scanned: dict[str, int] = {}

    def events(self, objects: list[Obj], store: Store) -> str:
        """The run's events: trip_events where dt is one of the objects' days and "$path" one of the objects."""
        days = ", ".join(f"'{d}'" for d in sorted({o.day for o in objects}))
        paths = ",\n    ".join(f"'{store.path(o.key)}'" for o in objects)
        return (f"(SELECT event_type, event_id, trip_id, pickup_zone_id, driver_id, fare,\n"
                f"         CAST(from_iso8601_timestamp(event_time) AS timestamp) AS event_time\n"
                f"  FROM {self.cfg.database}.trip_events\n"
                f"  WHERE dt IN ({days})\n"
                f"    AND \"$path\" IN (\n    {paths}))")

    def table(self, name: str) -> str:
        return f"{self.cfg.database}.{name}"

    def start(self, sql: str, step: str) -> str:
        try:
            return self.athena.start_query_execution(
                QueryString=sql, WorkGroup=self.cfg.workgroup,
                QueryExecutionContext={"Database": self.cfg.database})["QueryExecutionId"]
        except Exception as err:      # refused before it started: no query id
            raise StepFailed(step, "", f"{type(err).__name__}: {err}") from None

    def wait(self, qid: str, step: str) -> None:
        deadline = time.monotonic() + self.wait_s
        while True:
            q = self.athena.get_query_execution(QueryExecutionId=qid)["QueryExecution"]
            state = q["Status"]["State"]
            if state in ("SUCCEEDED", "FAILED", "CANCELLED"):
                break
            if time.monotonic() > deadline:
                raise StepFailed(step, qid, f"still {state} after {self.wait_s} s")
            self.sleep(2)
        self.scanned[qid] = q.get("Statistics", {}).get("DataScannedInBytes", 0)
        if state != "SUCCEEDED":
            raise StepFailed(step, qid, f"{state}: {q['Status'].get('StateChangeReason', '')}")

    def state(self, qid: str) -> str:
        return self.athena.get_query_execution(QueryExecutionId=qid)["QueryExecution"]["Status"]["State"]


class DuckEngine:
    """DuckDB in place of Athena, offline: the same MERGE texts, on local tables `trips` and `zone_counts`.

    `fail` makes a step fail (step name -> error text); `states` answers `state()` for a query id a scenario
    says is still running."""

    name = "duckdb"
    TABLES = {
        "trips": "trip_id VARCHAR, trip_state VARCHAR, pickup_zone_id INTEGER, requested_time TIMESTAMP, "
                 "request_event_id VARCHAR, driver_id VARCHAR, fare DOUBLE, last_event_time TIMESTAMP, "
                 "last_event_rank INTEGER, last_event_id VARCHAR",
        "zone_counts": "period_start DATE, pickup_zone_id INTEGER, trips_requested BIGINT, trips_completed BIGINT, "
                       "fare_total DOUBLE",
    }

    def __init__(self, path: str = ":memory:"):
        import duckdb
        self.con = duckdb.connect(path)
        for name, cols in self.TABLES.items():
            self.con.execute(f"CREATE TABLE IF NOT EXISTS {name} ({cols})")
        self.fail: dict[str, str] = {}
        self.states: dict[str, str] = {}
        self.started: list[tuple[str, str]] = []         # (step, sql) of every statement started
        self.n = 0

    def events(self, objects: list[Obj], store: Store) -> str:
        self.con.execute("CREATE OR REPLACE TEMP TABLE run_events (event_type VARCHAR, event_id VARCHAR, "
                         "trip_id VARCHAR, pickup_zone_id INTEGER, driver_id VARCHAR, fare DOUBLE, "
                         "event_time TIMESTAMP)")
        rows = [(d["event_type"], d["event_id"], d["trip_id"], d.get("pickup_zone_id"), d.get("driver_id"),
                 d.get("fare"), parse(d["event_time"]).replace(tzinfo=None)) for o in objects for d in o.lines]
        if rows:
            self.con.executemany("INSERT INTO run_events VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
        return "run_events"

    def table(self, name: str) -> str:
        return name

    def start(self, sql: str, step: str) -> str:
        self.n += 1
        qid = f"duck-{self.n}"
        self.started.append((step, sql))
        if step in self.fail:
            self.states[qid] = "FAILED"
            return qid
        self.con.execute(sql)
        self.states[qid] = "SUCCEEDED"
        return qid

    def wait(self, qid: str, step: str) -> None:
        if self.states.get(qid) == "FAILED":
            raise StepFailed(step, qid, f"FAILED: {self.fail[step]}")

    def state(self, qid: str) -> str:
        return self.states.get(qid, "SUCCEEDED")

    def rows(self, sql: str) -> list[tuple]:
        return self.con.execute(sql).fetchall()


# ---- the statements -------------------------------------------------------------------------------------------

def merge_sql(engine, objects: list[Obj], store: Store) -> str:
    source = sql_text("trips_source").format(events=engine.events(objects, store))
    return sql_text("merge_trips").format(trips=engine.table("trips"), source=source)


def period_start(day: date, period: str) -> date:
    if period == "week":
        return day - timedelta(days=day.weekday())          # weeks start on Monday, as date_trunc('week')
    if period == "month":
        return day.replace(day=1)
    return day


def next_period(start: date, period: str) -> date:
    if period == "week":
        return start + timedelta(days=7)
    if period == "month":
        return (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    return start + timedelta(days=1)


def rebuild_range(run_day: date, cfg: Config) -> tuple[date, date]:
    """[first, end) of the periods that hold a day from K + 1 days before the run's day to the run's day."""
    first = period_start(run_day - timedelta(days=cfg.k_days + 1), cfg.period)
    return first, next_period(period_start(run_day, cfg.period), cfg.period)


def rebuild_sql(engine, start_day: date, end_day: date, period: str) -> str:
    return sql_text("rebuild_counts").format(zone_counts=engine.table("zone_counts"), trips=engine.table("trips"),
                                             period=period, start_day=start_day.isoformat(),
                                             end_day=end_day.isoformat())


# ---- the run log ------------------------------------------------------------------------------------------------

class RunLog:
    """Items of job `merge` in `<PREFIX>-runs`: one per run (run_id = its start time), `lock` and `last-finished`."""

    def __init__(self, table, job: str = JOB):
        self.t, self.job = table, job

    def put(self, run_id: str, **attrs) -> None:
        self.t.put_item(Item={"job": self.job, "run_id": run_id, **attrs})

    def update(self, run_id: str, **attrs) -> None:
        names = sorted(attrs)
        self.t.update_item(Key={"job": self.job, "run_id": run_id},
                           UpdateExpression="SET " + ", ".join(f"{n} = :{n}" for n in names),
                           ExpressionAttributeValues={f":{n}": attrs[n] for n in names})

    def get(self, run_id: str) -> dict | None:
        return self.t.get_item(Key={"job": self.job, "run_id": run_id}).get("Item")

    def take_lock(self, run_id: str, now: datetime, lock_s: int) -> bool:
        from botocore.exceptions import ClientError
        try:
            self.t.put_item(Item={"job": self.job, "run_id": "lock", "lock_until": iso(now + timedelta(seconds=lock_s)),
                                  "holder": run_id},
                            ConditionExpression="attribute_not_exists(run_id) OR lock_until < :now",
                            ExpressionAttributeValues={":now": iso(now)})
            return True
        except ClientError as err:
            if err.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    def release(self, run_id: str) -> None:
        from botocore.exceptions import ClientError
        try:
            self.t.delete_item(Key={"job": self.job, "run_id": "lock"}, ConditionExpression="holder = :me",
                               ExpressionAttributeValues={":me": run_id})
        except ClientError as err:
            if err.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise

    def runs(self, limit: int = 25) -> list[dict]:
        """The newest run items, newest first (run ids are ISO times, which start with a digit)."""
        from boto3.dynamodb.conditions import Key
        return self.t.query(KeyConditionExpression=Key("job").eq(self.job) & Key("run_id").between("0", "9~"),
                            ScanIndexForward=False, Limit=limit)["Items"]


# ---- one run ----------------------------------------------------------------------------------------------------

def day_of(t: datetime) -> date:
    return t.astimezone(timezone.utc).date()


def open_days(store: Store, log: RunLog, run_day: date, cfg: Config) -> list[str]:
    """The arrival days whose folders this run lists (R81)."""
    first = run_day - timedelta(days=cfg.k_days + 1)
    last = log.get("last-finished")
    if last and last.get("window_end"):
        first = min(first, day_of(parse(last["window_end"])) - timedelta(days=1))
    elif cfg.merge_from:
        first = min(first, date.fromisoformat(cfg.merge_from))
    else:
        days = store.days()
        if days:
            first = min(first, date.fromisoformat(days[0]))
    n = (run_day - first).days
    return [(first + timedelta(days=i)).isoformat() for i in range(n + 1)]


def choose(store: Store, days: list[str], now: datetime, cfg: Config) -> list[Obj]:
    """The keys no manifest holds, settled for SETTLE_S, ordered by LastModified then key, at most MAX_OBJECTS."""
    settled = now - timedelta(seconds=cfg.settle_s)
    found = []
    for day in days:
        done = manifest(store, day)
        for key, modified in store.list(f"{RAW}dt={day}/"):
            if key not in done and modified <= settled:
                found.append(Obj(key, day, modified))
    found.sort(key=lambda o: (o.last_modified, o.key))
    return found[:cfg.max_objects]


def check(store: Store, obj: Obj) -> None:
    """Every line against the delivery model: valid ones into obj.lines, each bad one named in obj.bad."""
    text = store.read(obj.key) or ""
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            obj.lines.append(validate_line(line, DELIVERY, obj.key, n).model_dump(mode="json"))
        except LineError as err:
            obj.bad.append(f"line {err.line}, field {err.field}")


def run(now: datetime, *, store: Store, table, engine, cfg: Config | None = None, log=print) -> dict:
    """One scheduled run at `now`; returns its run-log item."""
    cfg = cfg or Config()
    runs = RunLog(table)
    run_id = iso(now)
    started = {"run_state": "running", "started_at": iso(datetime.now(timezone.utc)), "engine": engine.name}

    def skipped(why: str) -> dict:
        item = {"run_state": "skipped", "error_text": why, "started_at": started["started_at"],
                "ended_at": iso(datetime.now(timezone.utc))}
        runs.put(run_id, **item)
        log(f"{run_id} skipped: {why}")
        return {"run_id": run_id, **item}

    if not runs.take_lock(run_id, now, cfg.lock_s):
        return skipped("the lease is held by another run")
    for previous in runs.runs():
        if previous["run_state"] != "running" or previous["run_id"] == run_id:
            continue
        busy = [q for q in (previous.get("merge_query_id"), previous.get("rebuild_query_id"))
                if q and engine.state(q) in ("QUEUED", "RUNNING")]
        if busy:
            runs.release(run_id)
            return skipped(f"the run of {previous['run_id']} still runs query {busy[0]}")
        break                                     # only the newest run still marked running is looked at
    runs.put(run_id, **started)
    try:
        return _merge(now, run_id, store, runs, engine, cfg, log)
    finally:
        runs.release(run_id)


def _merge(now, run_id, store, runs, engine, cfg, log) -> dict:
    run_day = day_of(now)
    objects = choose(store, open_days(store, runs, run_day, cfg), now, cfg)
    for obj in objects:
        check(store, obj)
    bad = [o for o in objects if o.bad]
    good = [o for o in objects if not o.bad]
    for obj in bad:                                                   # R103: set aside whole
        store.copy(obj.key, f"{QUARANTINE}dt={obj.day}/{obj.key.rsplit('/', 1)[1]}")
    oldest_rebuilt = run_day - timedelta(days=cfg.k_days + 1)
    seen = {d["event_id"]: d for o in good for d in o.lines}
    old = sum(1 for d in seen.values() if day_of(parse(d["event_time"])) < oldest_rebuilt)
    item = {"objects": len(good), "quarantined": len(bad), "bad_lines": sum(len(o.bad) for o in bad),
            "old_events": old}
    if bad:
        item["bad_line_list"] = [f"{o.key}: {b}" for o in bad for b in o.bad][:50]
    if objects:
        item["window_start"] = iso(min(o.last_modified for o in objects))
        item["window_end"] = iso(max(o.last_modified for o in objects))
    runs.update(run_id, **item)
    step = "merge"
    try:
        if good:
            qid = engine.start(merge_sql(engine, good, store), "merge")
            item["merge_query_id"] = qid
            runs.update(run_id, merge_query_id=qid)
            engine.wait(qid, "merge")
            step = "rebuild"
            first, end = rebuild_range(run_day, cfg)
            qid = engine.start(rebuild_sql(engine, first, end, cfg.period), "rebuild")
            item["rebuild_query_id"] = qid
            runs.update(run_id, rebuild_query_id=qid)
            engine.wait(qid, "rebuild")
    except StepFailed as err:                                         # R101: nothing marked, next run merges again
        ended = {"run_state": "failed", "failed_step": err.step, "error_text": str(err)[:1000],
                 "ended_at": iso(datetime.now(timezone.utc))}
        runs.update(run_id, **ended)
        log(f"{run_id} failed in {err.step}: {err}")
        return {"run_id": run_id, **item, **ended, "taken": [o.key for o in objects]}
    by_day: dict[str, list[str]] = {}
    for obj in objects:
        by_day.setdefault(obj.day, []).append(f"{obj.key}\t{'quarantined' if obj.bad else 'merged'}")
    for day, lines in sorted(by_day.items()):                         # one PUT a day
        old_text = store.read(f"{MANIFESTS}dt={day}.txt") or ""
        store.put(f"{MANIFESTS}dt={day}.txt", old_text + "".join(line + "\n" for line in lines))
    if objects:
        runs.put("last-finished", window_end=item["window_end"], finished_run=run_id)
    ended = {"run_state": "finished", "ended_at": iso(datetime.now(timezone.utc))}
    runs.update(run_id, **ended)
    log(f"{run_id} finished: {len(good)} objects merged, {len(bad)} quarantined, {old} old events")
    return {"run_id": run_id, **item, **ended, "taken": [o.key for o in objects]}


def handler(event, context):
    """The Lambda entry point: the schedule's time (or now) as the run's time."""
    import boto3
    cfg = Config()
    now = parse(event["time"]) if isinstance(event, dict) and event.get("time") else datetime.now(timezone.utc)
    store = Store(boto3.client("s3"), cfg.bucket)
    table = boto3.resource("dynamodb").Table(cfg.run_log)
    item = run(now, store=store, table=table, engine=AthenaEngine(boto3.client("athena"), cfg), cfg=cfg)
    item.pop("taken", None)
    print(json.dumps(item, default=str))
    if item["run_state"] == "failed":
        raise RuntimeError(f"merge run {item['run_id']} failed in {item['failed_step']}: {item['error_text']}")
    return item

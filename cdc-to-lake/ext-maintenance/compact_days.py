"""The nightly compaction by a rule: count the data files of each day, compact the closed days over the limit.

This file is the Lambda function `<PREFIX>-compact-days` (handler `compact_days.handler`, boto3 only), called by
two schedules of the group `<PREFIX>-maintenance-compaction`: at 03:00 Vancouver time with {"mode": "compact"}, at
04:00 with {"mode": "vacuum"}. `maintenance.py` deploys it; the scenarios call `nightly()` with moto and a recording
stand-in for Athena.

A compact run, in this order:
  1. refuses a raw table with partition projection on, or whose files are not Parquet, before it changes anything
     (R130)
  2. takes the lease (item `lock` of job `compact-days` in `<PREFIX>-runs`), or stops
  3. finishes the raw deletes a stopped run left `started`, then copies each late file of a moved day into the
     day's location and deletes the raw files of days moved more than 24 hours before (R134)
  4. counts the data files of each day of the last 30 days: `"<table>$partitions"` for an Iceberg table, an S3 listing
     of the day's location for `trip_events_parquet`, and writes the counts to the run log (R120)
  5. compacts the closed days over MAX_FILES_PER_QUERY / QUERY_DAYS, at most COMPACT_MAX_DAYS, oldest first (R121):
     Iceberg by `OPTIMIZE ... WHERE <that day>` (R122), Parquet by a CTAS under a new prefix and a switch of the
     day's partition when the counts agree (R132, R133)
  6. registers the partitions of the next 7 days at their raw folders (R140)
  7. raises one error naming every refused OPTIMIZE and every table it could not count, so Lambda counts the run
     in Errors (R122, R141)
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

JOB = "compact-days"
RAW = "raw/trip-events/"
COMPACTED = "compacted/trip-events/"
INPUTS = "maintenance/inputs/"
MANIFESTS = "lake/merge-state/"
COUNT_DAYS = 30                 # ours: the days counted each night, the 30-day count's window
LATE_WINDOW_DAYS = 30           # ours: a moved day is checked for late files for 30 days
DELETE_AFTER_H = 24             # ours: a query that started on the old location can finish
DELETE_BATCH = 1000             # DeleteObjects takes up to 1,000 keys a call (S3 API reference)
LOCK_S = 900
REGISTER_AHEAD = 7
DAY_FIELDS = {"trip_changes": "change_time", "trips": "requested_time"}
PROJECTION_ERROR = "{table} uses partition projection: run maintenance.py --register-partitions first"
FORMAT_ERROR = "{table} is not Parquet: a CTAS would write another format into it"


def iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


@dataclass(frozen=True)
class Config:
    prefix: str = field(default_factory=lambda: os.environ.get("PREFIX") or "recipe")
    bucket: str = field(default_factory=lambda: os.environ.get("BUCKET", ""))
    max_files_per_query: int = field(default_factory=lambda: int(os.environ.get("MAX_FILES_PER_QUERY", "3000")))
    query_days: int = field(default_factory=lambda: int(os.environ.get("QUERY_DAYS", "30")))
    close_after_hours: int = field(default_factory=lambda: int(os.environ.get("CLOSE_AFTER_HOURS", "1")))
    compact_max_days: int = field(default_factory=lambda: int(os.environ.get("COMPACT_MAX_DAYS", "10")))
    merge_reads_raw: bool = field(default_factory=lambda: os.environ.get("MERGE_READS_RAW", "0") == "1")
    raw_table_env: str = field(default_factory=lambda: os.environ.get("RAW_TABLE", ""))
    iceberg_env: str = field(default_factory=lambda: os.environ.get("ICEBERG_TABLES", ""))

    @property
    def database(self) -> str:
        return f"{self.prefix.replace('-', '_')}_lake"

    @property
    def raw_table(self) -> tuple[str, str]:
        name = self.raw_table_env or f"{self.database}.trip_events_parquet"
        db, _, table = name.rpartition(".")
        return db or self.database, table

    @property
    def iceberg_tables(self) -> list[tuple[str, str, str | None]]:
        """(database, table, day column or None) of ICEBERG_TABLES: `db.table[:column]`, comma-separated; the
        defaults are the main scenario's change log and scheduled-merge's trips."""
        text = self.iceberg_env or (f"{self.prefix.replace('-', '_')}_cdc.trip_changes:change_time,"
                                    f"{self.database}.trips:requested_time")
        out = []
        for part in [p.strip() for p in text.split(",") if p.strip()]:
            name, _, column = part.partition(":")
            db, _, table = name.rpartition(".")
            out.append((db or self.database, table, column or (None if ":" in part else DAY_FIELDS.get(table))))
        return out

    @property
    def limit(self) -> int:
        """Files a day a query over QUERY_DAYS may read: MAX_FILES_PER_QUERY / QUERY_DAYS, rounded down."""
        return self.max_files_per_query // self.query_days

    @property
    def workgroup(self) -> str:
        return f"{self.prefix}-wg"

    @property
    def ctas_workgroup(self) -> str:
        return f"{self.prefix}-ctas-wg"


class AthenaError(RuntimeError):
    pass


class Athena:
    """Athena: run one statement in a workgroup and wait; rows of a SELECT as dicts."""

    def __init__(self, client, sleep=time.sleep, wait_s: int = 600):
        self.c, self.sleep, self.wait_s = client, sleep, wait_s
        self.spent: list[dict] = []          # every statement's Data scanned, failed ones included

    def run(self, sql: str, workgroup: str, database: str | None = None) -> list[dict]:
        kw = {"QueryString": sql, "WorkGroup": workgroup}
        if database:
            kw["QueryExecutionContext"] = {"Database": database}
        qid = self.c.start_query_execution(**kw)["QueryExecutionId"]
        deadline = time.monotonic() + self.wait_s
        while True:
            q = self.c.get_query_execution(QueryExecutionId=qid)["QueryExecution"]
            state = q["Status"]["State"]
            if state in ("SUCCEEDED", "FAILED", "CANCELLED"):
                break
            if time.monotonic() > deadline:
                raise AthenaError(f"query {qid} still {state} after {self.wait_s} s")
            self.sleep(1)
        stats = q.get("Statistics", {})
        self.spent.append({"sql": sql.split("\n")[0][:80], "state": state,
                           "scanned": stats.get("DataScannedInBytes", 0),
                           "ms": stats.get("EngineExecutionTimeInMillis", 0)})
        if state != "SUCCEEDED":
            raise AthenaError(f"{state}: {q['Status'].get('StateChangeReason', '')}")
        if not sql.lstrip().upper().startswith(("SELECT", "WITH")):
            return []
        rows, header = [], None
        for page in self.c.get_paginator("get_query_results").paginate(QueryExecutionId=qid):
            for r in page["ResultSet"]["Rows"]:
                vals = [c.get("VarCharValue") for c in r["Data"]]
                if header is None:
                    header = vals
                else:
                    rows.append(dict(zip(header, vals)))
        return rows


# ---- the run log --------------------------------------------------------------------------------------------------

class Log:
    def __init__(self, table):
        self.t = table

    def put(self, run_id: str, **attrs):
        self.t.put_item(Item={"job": JOB, "run_id": run_id, **attrs})

    def update(self, run_id: str, **attrs):
        names = sorted(attrs)
        self.t.update_item(Key={"job": JOB, "run_id": run_id},
                           UpdateExpression="SET " + ", ".join(f"{n} = :{n}" for n in names),
                           ExpressionAttributeValues={f":{n}": attrs[n] for n in names})

    def get(self, run_id: str) -> dict | None:
        return self.t.get_item(Key={"job": JOB, "run_id": run_id}).get("Item")

    def moved_days(self) -> list[dict]:
        """The items `moved-<day>`: one per day of trip_events_parquet whose partition was switched."""
        from boto3.dynamodb.conditions import Key
        return self.t.query(KeyConditionExpression=Key("job").eq(JOB) & Key("run_id").begins_with("moved-"))["Items"]

    def take_lock(self, run_id: str, now: datetime) -> bool:
        from botocore.exceptions import ClientError
        try:
            self.t.put_item(Item={"job": JOB, "run_id": "lock", "lock_until": iso(now + timedelta(seconds=LOCK_S)),
                                  "holder": run_id},
                            ConditionExpression="attribute_not_exists(run_id) OR lock_until < :now",
                            ExpressionAttributeValues={":now": iso(now)})
            return True
        except ClientError as err:
            if err.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    def release(self, run_id: str):
        from botocore.exceptions import ClientError
        try:
            self.t.delete_item(Key={"job": JOB, "run_id": "lock"}, ConditionExpression="holder = :me",
                               ExpressionAttributeValues={":me": run_id})
        except ClientError as err:
            if err.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise


# ---- S3 and Glue ----------------------------------------------------------------------------------------------

def s3_url(bucket: str, key: str) -> str:
    return f"s3://{bucket}/{key}"


def key_of(url: str) -> tuple[str, str]:
    rest = url.removeprefix("s3://")
    bucket, _, key = rest.partition("/")
    return bucket, key


def listing(s3, bucket: str, prefix: str) -> dict[str, int]:
    """key -> size of every object under a prefix (ListObjectsV2, 1,000 keys a page)."""
    out = {}
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        out.update({o["Key"]: o["Size"] for o in page.get("Contents", [])})
    return out


def delete_keys(s3, bucket: str, keys: list[str]) -> None:
    for i in range(0, len(keys), DELETE_BATCH):
        s3.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": k} for k in keys[i:i + DELETE_BATCH]],
                                                 "Quiet": True})


def raw_folder(bucket: str, day: str) -> str:
    return s3_url(bucket, f"{RAW}dt={day}/")


def partition_location(glue, db: str, table: str, day: str) -> str | None:
    try:
        p = glue.get_partition(DatabaseName=db, TableName=table, PartitionValues=[day])["Partition"]
        return p["StorageDescriptor"]["Location"]
    except glue.exceptions.EntityNotFoundException:
        return None


def set_location(glue, db: str, table: str, day: str, location: str) -> None:
    """One catalog change (Glue UpdatePartition): a query reads the old files or the new ones, never both."""
    p = glue.get_partition(DatabaseName=db, TableName=table, PartitionValues=[day])["Partition"]
    sd = dict(p["StorageDescriptor"], Location=location)
    glue.update_partition(DatabaseName=db, TableName=table, PartitionValueList=[day],
                          PartitionInput={"Values": [day], "StorageDescriptor": sd,
                                          "Parameters": p.get("Parameters", {})})


def register(glue, db: str, table: str, bucket: str, days: list[str]) -> list[str]:
    """Register the partitions of `days` that are not registered, each at its raw folder; returns those added."""
    t = glue.get_table(DatabaseName=db, Name=table)["Table"]
    have = set()
    for page in glue.get_paginator("get_partitions").paginate(DatabaseName=db, TableName=table):
        have |= {p["Values"][0] for p in page["Partitions"]}
    new = [d for d in days if d not in have]
    sd = t["StorageDescriptor"]
    for i in range(0, len(new), 100):          # BatchCreatePartition takes up to 100 partitions a call
        glue.batch_create_partition(DatabaseName=db, TableName=table, PartitionInputList=[
            {"Values": [d], "StorageDescriptor": dict(sd, Location=raw_folder(bucket, d))} for d in new[i:i + 100]])
    return new


def check_raw_table(glue, db: str, table: str) -> None:
    """R130: stop before anything changes when projection is on or the files are not Parquet."""
    t = glue.get_table(DatabaseName=db, Name=table)["Table"]
    if t.get("Parameters", {}).get("projection.enabled", "false").lower() == "true":
        raise RefusedTable(PROJECTION_ERROR.format(table=table))
    sd = t.get("StorageDescriptor", {})
    serde = sd.get("SerdeInfo", {}).get("SerializationLibrary", "") + sd.get("InputFormat", "")
    if "parquet" not in serde.lower():
        raise RefusedTable(FORMAT_ERROR.format(table=table))


class RefusedTable(RuntimeError):
    pass


def merge_manifest(s3, bucket: str, day: str) -> dict[str, str] | None:
    """scheduled-merge's manifest of an arrival day (key -> merged | quarantined), or None when there is none."""
    try:
        text = s3.get_object(Bucket=bucket, Key=f"{MANIFESTS}dt={day}.txt")["Body"].read().decode()
    except s3.exceptions.NoSuchKey:
        return None
    return dict(line.split("\t", 1) for line in text.splitlines() if "\t" in line)


# ---- the nightly run ------------------------------------------------------------------------------------------

def closed(day: date, now: datetime, cfg: Config) -> bool:
    end = datetime(day.year, day.month, day.day, tzinfo=timezone.utc) + timedelta(days=1)
    return now >= end + timedelta(hours=cfg.close_after_hours)


def day_bounds(day: str) -> tuple[str, str]:
    d = date.fromisoformat(day)
    return f"{d} 00:00:00", f"{d + timedelta(days=1)} 00:00:00"


def optimize_sql(db: str, table: str, column: str | None, day: str | None) -> str:
    sql = f"OPTIMIZE {db}.{table} REWRITE DATA USING BIN_PACK"
    if column and day:
        start, end = day_bounds(day)
        sql += f" WHERE {column} >= TIMESTAMP '{start}' AND {column} < TIMESTAMP '{end}'"
    return sql


def files_sql(db: str, table: str, column: str | None) -> str:
    """Data files per day from the table's `$partitions` metadata table: in Athena its `partition` row names a day
    partition `<column>_day` (a date) and `file_count` counts the day's data files; `$files` has no partition
    column (both seen in Athena, output/athena-probes.txt). One count from `$files` for a table without a day
    partition (it lists data files only, not delete files)."""
    if column:
        return (f'SELECT CAST(partition.{column}_day AS varchar) AS day, file_count AS files '
                f'FROM "{db}"."{table}$partitions" ORDER BY 1')
    return f'SELECT count(*) AS files FROM "{db}"."{table}$files"'


def ctas_name(day: str, tag: str) -> str:
    return f"compact_{day.replace('-', '')}_{tag.lower()}"


def run_tag(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


class Night:
    """One run: the clients, the config, the run's item and what it did."""

    def __init__(self, now, cfg, s3, glue, table, athena, log=print, remaining_s=lambda: 10 ** 9):
        self.now, self.cfg, self.s3, self.glue, self.athena = now, cfg, s3, glue, athena
        self.log, self.runs, self.remaining_s = log, Log(table), remaining_s
        self.run_id, self.tag = iso(now), run_tag(now)
        self.item: dict = {}
        self.refused: list[str] = []
        self.errors: list[str] = []

    # -- R134 ----------------------------------------------------------------------------------------------
    def finish_started_deletes(self) -> None:
        for moved in self.runs.moved_days():
            if moved.get("raw_delete") == "started":
                self.delete_raw(moved)

    def delete_raw(self, moved: dict) -> None:
        listed = json.loads(self.s3.get_object(Bucket=self.cfg.bucket, Key=moved["inputs_key"])["Body"].read())
        self.runs.update(moved["run_id"], raw_delete="started")
        delete_keys(self.s3, self.cfg.bucket, sorted(listed["files"]))
        self.runs.update(moved["run_id"], raw_delete="done", raw_deleted_at=self.run_id)
        self.log(f"deleted  the {len(listed['files'])} raw files of {moved['moved_day']}")

    def late_files_and_deletes(self) -> None:
        bucket, db, table = self.cfg.bucket, *self.cfg.raw_table
        for moved in sorted(self.runs.moved_days(), key=lambda m: m["moved_day"]):
            moved_at = datetime.fromisoformat(moved["moved_at"].replace("Z", "+00:00"))
            if self.now - moved_at > timedelta(days=LATE_WINDOW_DAYS) or moved.get("raw_delete") == "done":
                continue
            day = moved["moved_day"]
            listed = json.loads(self.s3.get_object(Bucket=bucket, Key=moved["inputs_key"])["Body"].read())["files"]
            raw = listing(self.s3, bucket, f"{RAW}dt={day}/")
            manifest = merge_manifest(self.s3, bucket, day) if self.cfg.merge_reads_raw else None
            location = partition_location(self.glue, db, table, day)
            copied = []
            for key in sorted(set(raw) - set(listed)):
                if self.cfg.merge_reads_raw and (manifest or {}).get(key) != "merged":
                    continue                     # the merge has not taken it: it must stay where the merge reads
                lbucket, lprefix = key_of(location)
                to = f"{lprefix}late-{key.rsplit('/', 1)[1]}"
                self.s3.copy_object(Bucket=lbucket, Key=to, CopySource={"Bucket": bucket, "Key": key})
                self.s3.delete_object(Bucket=bucket, Key=key)
                copied.append(to)
            if copied:
                n = int(moved.get("late_files_copied", 0)) + len(copied)
                self.runs.update(moved["run_id"], late_files_copied=n, last_late_copy=self.run_id)
                self.item.setdefault("late_copies", []).extend(copied)
                self.log(f"copied   {len(copied)} late file(s) into the location of {day}")
            if self.now - moved_at < timedelta(hours=DELETE_AFTER_H):
                continue
            if self.cfg.merge_reads_raw:
                if manifest is None:
                    self.runs.update(moved["run_id"], raw_kept="no merge manifest of the day yet")
                    continue
                not_merged = [k for k in listed if manifest.get(k) != "merged"]
                if not_merged:
                    self.runs.update(moved["run_id"], raw_kept=f"{len(not_merged)} listed files not merged "
                                     f"(quarantined or not taken yet), first {not_merged[0]}")
                    continue
            self.delete_raw(moved)

    # -- R120, R121 ------------------------------------------------------------------------------------------
    def count(self) -> dict[tuple[str, str, str | None], dict[str, int]]:
        cfg, counts = self.cfg, {}
        first = (self.now - timedelta(days=COUNT_DAYS)).date()
        for db, table, column in cfg.iceberg_tables:
            try:
                rows = self.athena.run(files_sql(db, table, column), cfg.workgroup, db)
            except AthenaError as err:
                self.errors.append(f"{db}.{table}: counting files: {err}")
                continue
            if column:
                counts[(db, table, column)] = {r["day"]: int(r["files"]) for r in rows
                                               if r["day"] and date.fromisoformat(r["day"]) >= first}
            else:
                counts[(db, table, None)] = {"whole table": int(rows[0]["files"]) if rows else 0}
        db, table = cfg.raw_table
        parquet = {}
        for i in range(COUNT_DAYS + 1):
            day = (first + timedelta(days=i)).isoformat()
            location = partition_location(self.glue, db, table, day)
            if location:
                b, p = key_of(location)
                n = len(listing(self.s3, b, p))
                if n:
                    parquet[day] = n
        counts[(db, table, "dt")] = parquet
        self.item["files_by_day"] = {f"{d}.{t}": v for (d, t, _), v in counts.items()}
        return counts

    def decide(self, counts) -> list[tuple[str, str, str | None, str]]:
        """(db, table, column, day) to compact, oldest first, at most COMPACT_MAX_DAYS."""
        marked = []
        for (db, table, column), days in counts.items():
            if column is None:
                if days.get("whole table", 0) > self.cfg.max_files_per_query:
                    marked.append(("0000-00-00", db, table, None, None))
                continue
            for day, n in days.items():
                if n > self.cfg.limit and closed(date.fromisoformat(day), self.now, self.cfg):
                    marked.append((day, db, table, column, day))
        marked.sort()
        chosen = marked[:self.cfg.compact_max_days]
        self.item["limit_per_day"] = self.cfg.limit
        self.item["marked"] = [f"{db}.{t} {d or 'whole table'}" for _, db, t, _, d in chosen]
        self.item["waiting"] = [f"{db}.{t} {d or 'whole table'}" for _, db, t, _, d in marked[len(chosen):]]
        return [(db, t, c, d) for _, db, t, c, d in chosen]

    # -- R122 ----------------------------------------------------------------------------------------------
    def optimize(self, db: str, table: str, column: str | None, day: str | None) -> None:
        if f"{db}.{table}" in self.refused:
            return                               # refused once tonight: no other OPTIMIZE of that table
        try:
            self.athena.run(optimize_sql(db, table, column, day), self.cfg.workgroup, db)
            self.log(f"optimized {db}.{table} {day or 'whole table'}")
        except AthenaError as err:
            self.refused.append(f"{db}.{table}")
            self.item.update(failed_step="optimize", error_text=f"{db}.{table} {day}: {err}"[:1000])
            self.log(f"REFUSED  OPTIMIZE of {db}.{table} {day}: {err}")

    # -- R132, R133 ----------------------------------------------------------------------------------------
    def copy_day(self, day: str, after_list=None) -> bool:
        cfg, s3, glue = self.cfg, self.s3, self.glue
        db, table = cfg.raw_table
        old = partition_location(glue, db, table, day)
        ob, op = key_of(old)
        files = listing(s3, ob, op)
        inputs_key = f"{INPUTS}dt={day}/{self.tag}.json"
        s3.put_object(Bucket=cfg.bucket, Key=inputs_key,
                      Body=json.dumps({"day": day, "location": old, "files": files}, sort_keys=True).encode())
        new_prefix = f"{COMPACTED}dt={day}/{self.tag}/"
        new = s3_url(cfg.bucket, new_prefix)
        name = ctas_name(day, self.tag)
        switched = False
        try:
            cols = [c["Name"] for c in glue.get_table(DatabaseName=db, Name=table)["Table"]["StorageDescriptor"]["Columns"]]
            self.athena.run(f"CREATE TABLE {db}.{name} WITH (format = 'PARQUET', write_compression = 'ZSTD', "
                            f"external_location = '{new}', bucketed_by = ARRAY['trip_id'], bucket_count = 1) AS "
                            f"SELECT {', '.join(cols)} FROM {db}.{table} WHERE dt = '{day}'", cfg.ctas_workgroup, db)
            if after_list:
                after_list(day)
            if listing(s3, ob, op) != files:
                raise CopyFailed("files changed during the copy")
            got = int(self.athena.run(f"SELECT count(*) AS n FROM {db}.{name}", cfg.workgroup, db)[0]["n"])
            want = int(self.athena.run(f"SELECT count(*) AS n FROM {db}.{table} WHERE dt = '{day}'",
                                       cfg.workgroup, db)[0]["n"])
            if got != want:
                raise CopyFailed(f"{got} rows, expected {want}")
            set_location(glue, db, table, day, new)
            switched = True
            row = self.athena.run(f"SELECT count(*) AS n, count_if(\"$path\" NOT LIKE '{s3_url(cfg.bucket, COMPACTED)}%') "
                                  f"AS outside FROM {db}.{table} WHERE dt = '{day}'", cfg.workgroup, db)[0]
            if int(row["n"]) != want or int(row["outside"]) != 0:
                raise CopyFailed(f"after the switch {row['n']} rows ({row['outside']} outside compacted/), "
                                 f"expected {want}")
        except (CopyFailed, AthenaError) as err:
            if switched:
                set_location(glue, db, table, day, old)
            delete_keys(s3, cfg.bucket, sorted(listing(s3, cfg.bucket, new_prefix)))
            self._drop(db, name)
            self.item.update(failed_step="copy", error_text=f"{day}: {err}"[:1000])
            self.item.setdefault("failed_days", []).append(f"{day}: {err}")
            self.log(f"FAILED   copy of {day}: {err}; the partition stays at {old}")
            return False
        self._drop(db, name)
        self.runs.put(f"moved-{day}", moved_day=day, moved_at=self.run_id, location=new, old_location=old,
                      inputs_key=inputs_key, rows_before=want, rows_after=int(row["n"]), table_name=f"{db}.{table}",
                      late_files_copied=0, raw_delete="waiting")
        self.item.setdefault("moved_days", []).append(day)
        self.log(f"moved    {day}: {len(files)} files to {new} ({want} rows before and after)")
        return True

    def _drop(self, db: str, name: str) -> None:
        """The CTAS table goes; its files stay (they are the day's new location)."""
        try:
            self.glue.delete_table(DatabaseName=db, Name=name)
        except self.glue.exceptions.EntityNotFoundException:
            pass

    # -- R140 ----------------------------------------------------------------------------------------------
    def register_ahead(self) -> list[str]:
        db, table = self.cfg.raw_table
        today = self.now.date()
        days = [(today + timedelta(days=i)).isoformat() for i in range(1, REGISTER_AHEAD + 1)]
        return register(self.glue, db, table, self.cfg.bucket, days)


class CopyFailed(RuntimeError):
    pass


def nightly(now: datetime, cfg: Config, *, s3, glue, table, athena, log=print, after_list=None,
            remaining_s=lambda: 10 ** 9) -> dict:
    """One compact run at `now`; returns its run-log item. Raises RefusedTable (R130) before any change, and
    RuntimeError at the end when an OPTIMIZE was refused (R122)."""
    night = Night(now, cfg, s3, glue, table, athena, log, remaining_s)
    db, raw = cfg.raw_table
    check_raw_table(glue, db, raw)
    if not night.runs.take_lock(night.run_id, now):
        night.runs.put(night.run_id, run_state="skipped", error_text="the lease is held by another run")
        return {"run_id": night.run_id, "run_state": "skipped"}
    night.runs.put(night.run_id, run_state="running", started_at=iso(datetime.now(timezone.utc)))
    try:
        night.finish_started_deletes()
        night.late_files_and_deletes()
        counts = night.count()
        night.runs.update(night.run_id, files_by_day=night.item["files_by_day"])
        for db_, table_, column, day in night.decide(counts):
            if night.remaining_s() < 120:
                night.item.setdefault("waiting", []).append(f"{db_}.{table_} {day}: under 120 s left")
                continue
            if (db_, table_) == (db, raw):
                night.copy_day(day, after_list)
            else:
                night.optimize(db_, table_, column, day)
        night.item["registered"] = night.register_ahead()
        state = "failed" if night.refused or night.item.get("failed_days") or night.errors else "finished"
        if night.errors:
            night.item.setdefault("error_text", "; ".join(night.errors)[:1000])
        night.runs.update(night.run_id, run_state=state, ended_at=iso(datetime.now(timezone.utc)),
                          **{k: v for k, v in night.item.items() if k != "files_by_day" and v != []})
    finally:
        night.runs.release(night.run_id)
    result = {"run_id": night.run_id, "run_state": state, **night.item}
    if night.refused or night.errors:        # raised, so Lambda counts the run in Errors and the alarm sends (R141)
        parts = ([f"OPTIMIZE refused for {', '.join(night.refused)}: {night.item.get('error_text')}"]
                 if night.refused else []) + night.errors
        raise RuntimeError("; ".join(parts))
    return result


def vacuum(cfg: Config, athena, log=print) -> list[str]:
    done = []
    for db, table, _ in cfg.iceberg_tables:
        athena.run(f"VACUUM {db}.{table}", cfg.workgroup, db)
        done.append(f"{db}.{table}")
        log(f"vacuumed {db}.{table}")
    return done


def handler(event, context):
    import boto3
    cfg = Config()
    athena = Athena(boto3.client("athena"))
    if (event or {}).get("mode") == "vacuum":
        return {"vacuumed": vacuum(cfg, athena)}
    now = datetime.now(timezone.utc)
    remaining = (lambda: context.get_remaining_time_in_millis() / 1000) if context else (lambda: 10 ** 9)
    item = nightly(now, cfg, s3=boto3.client("s3"), glue=boto3.client("glue"),
                   table=boto3.resource("dynamodb").Table(f"{cfg.prefix}-runs"), athena=athena, remaining_s=remaining)
    print(json.dumps(item, default=str))
    return item

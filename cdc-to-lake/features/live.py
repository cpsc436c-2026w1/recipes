"""The examples tagged @live: they run against AWS, not moto, and only with `--tags=@live`.

    export PREFIX=recipe-yourname                        # a sandbox terminal, ca-central-1
    uv run behave --tags=@live                           # about 25 minutes; under 0.40 USD (README, Cost)
    uv run python features/live.py down                  # what the examples left: nothing is deleted
    uv run python features/live.py down --yes            # delete it

The examples use their own prefix, `$PREFIX-live`, so they never touch a team's lake: the bucket
`$PREFIX-live-lake-<account>`, the workgroups `$PREFIX-live-wg` and `$PREFIX-live-ctas-wg`, the run log
`$PREFIX-live-runs` and the Glue database `${prefix}_live_lake`. Each example starts by putting its own data set in
place: the day folders of `raw/trip-events/` it does not name are deleted, a day folder that already holds the data
set (`live-fixtures/dt=<day>.json` says which) is kept, `trip_events_parquet` is made again with no partition, and
`compacted/`, `maintenance/` and the run log items of job `compact-days` are deleted. The thirty days of SIM01
(43,200 small files, about 0.24 USD of PUT requests) are landed once and kept for the next run until `down --yes`.

`down` writes what Athena scanned in the two workgroups (every query, from GetQueryExecution) to
output/behave-live-cost.txt before it deletes them.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path += [p for p in [str(ROOT / "ext-maintenance"), str(ROOT / "ext-scheduled-merge"), str(ROOT / "scripts"),
                         str(ROOT / "sim"), str(ROOT / "src"), str(ROOT)] if p not in sys.path]

REGION = "ca-central-1"
MASK = "111122223333"
RAW = "raw/trip-events/"
MARKS = "live-fixtures/"
ROWS_PER_FILE = 12
SUFFIX = "-live"
_TEMPLATE: list[dict] = []
REAL_ENV: dict[str, str] = {}


def use_live_env() -> str:
    """The shell's environment (as it was before any offline scenario changed it), with PREFIX=$PREFIX-live."""
    os.environ.clear()
    os.environ.update(REAL_ENV)
    base = os.environ.get("PREFIX", "")
    if not base or base == "recipe":
        raise RuntimeError("the @live examples need PREFIX set to your own prefix (they use $PREFIX-live)")
    if base.endswith(SUFFIX):
        raise RuntimeError(f"PREFIX={base} ends in {SUFFIX}: set your own prefix; the examples add {SUFFIX}")
    os.environ["PREFIX"] = base + SUFFIX
    os.environ.setdefault("AWS_REGION", REGION)
    os.environ.setdefault("AWS_DEFAULT_REGION", REGION)
    os.environ.pop("LEDGER", None)                 # the real ledger, state/ledger.jsonl
    return os.environ["PREFIX"]


def session():
    import boto3
    return boto3.session.Session(region_name=REGION)


def template() -> list[dict]:
    """SIM01's deliveries: the rows of every file the examples land."""
    if not _TEMPLATE:
        import sim_data
        _TEMPLATE.extend(sim_data.load("SIM01")[0])
    return _TEMPLATE


def put_all(s3, bucket: str, bodies: dict[str, bytes]) -> None:
    with ThreadPoolExecutor(32) as pool:
        list(pool.map(lambda kv: s3.put_object(Bucket=bucket, Key=kv[0], Body=kv[1]), bodies.items()))


def delete_prefix(s3, bucket: str, prefix: str) -> int:
    import compact_days as cd
    keys = sorted(cd.listing(s3, bucket, prefix))
    cd.delete_keys(s3, bucket, keys)
    return len(keys)


class LiveLake:
    """The same parts as the offline Lake of compaction_steps.py, in AWS: real S3, Glue, DynamoDB and Athena."""

    def __init__(self, context, night_at: datetime):
        import compact_days as cd
        import lake
        import nightly_ops
        import run_log

        self.cd, self.context, self.night_at = cd, context, night_at
        sess = session()
        self.sess = sess
        lake.up(sess, log=lambda *_: None)
        for suffix in ("wg", "ctas-wg"):
            lake.ensure_workgroup(f"{os.environ['PREFIX']}-{suffix}", sess=sess)
        run_log.ensure_run_log(sess)
        self.bucket = lake.bucket_name(sess)
        self.s3, self.glue = sess.client("s3"), sess.client("glue")
        self.table = sess.resource("dynamodb").Table(run_log.run_log_name())
        self.athena = cd.Athena(sess.client("athena"))
        self.db, self.raw = cd.Config().raw_table
        self.iceberg = ","                              # no Iceberg table unless an example makes one
        self.items: list[dict] = []
        self.after_list, self.error = None, None
        self.marks: dict = {}
        self.printed: list[str] = []
        self.reset()
        old = nightly_ops.clock
        nightly_ops.clock = lambda: night_at
        context.add_cleanup(setattr, nightly_ops, "clock", old)

    # -- the example's starting point ----------------------------------------------------------------------
    def reset(self) -> None:
        import nightly_ops
        from boto3.dynamodb.conditions import Key
        items = self.table.query(KeyConditionExpression=Key("job").eq(self.cd.JOB))["Items"]
        with self.table.batch_writer() as batch:
            for item in items:
                batch.delete_item(Key={"job": item["job"], "run_id": item["run_id"]})
        for prefix in (self.cd.COMPACTED, "maintenance/"):
            delete_prefix(self.s3, self.bucket, prefix)
        try:
            for t in self.glue.get_tables(DatabaseName=self.db)["TableList"]:
                if t["Name"] == self.raw or t["Name"].startswith("compact_"):
                    self.glue.delete_table(DatabaseName=self.db, Name=t["Name"])
        except self.glue.exceptions.EntityNotFoundException:
            pass
        nightly_ops.ensure_raw_table(self.glue, self.cfg(), self.bucket)

    def cfg(self):
        return self.cd.Config(bucket=self.bucket, iceberg_env=self.iceberg)

    def days_present(self) -> list[str]:
        out = []
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=RAW, Delimiter="/"):
            out += [p["Prefix"][len(RAW) + 3:-1] for p in page.get("CommonPrefixes", [])]
        return sorted(out)

    def land_set(self, name: str, plan: dict[str, int], rows) -> int:
        """Put the data set `name` in raw/trip-events/: `plan` is day -> files, `rows(day, i)` the rows of file i.
        A day folder that holds this set already is kept; one of another set, or not in the plan, is deleted.
        Every day of the plan is registered at its raw folder. Returns the files written."""
        for day in self.days_present():
            if day not in plan:
                delete_prefix(self.s3, self.bucket, f"{RAW}dt={day}/")
                self.s3.delete_object(Bucket=self.bucket, Key=f"{MARKS}dt={day}.json")
        written = 0
        for day, n in sorted(plan.items()):
            want = {self.key(day, i) for i in range(n)}
            have = set(self.keys(f"{RAW}dt={day}/"))
            try:
                mark = json.loads(self.s3.get_object(Bucket=self.bucket, Key=f"{MARKS}dt={day}.json")["Body"].read())
            except self.s3.exceptions.NoSuchKey:
                mark = {}
            if have == want and mark.get("set") == name:
                continue
            delete_prefix(self.s3, self.bucket, f"{RAW}dt={day}/")
            self.write(day, range(n), rows)
            self.s3.put_object(Bucket=self.bucket, Key=f"{MARKS}dt={day}.json",
                               Body=json.dumps({"set": name, "files": n}).encode())
            written += n
        self.cd.register(self.glue, self.db, self.raw, self.bucket, sorted(plan))
        self.printed.append(f"data set {name}: {sum(plan.values())} files over {len(plan)} days, {written} written now")
        return written

    def key(self, day: str, i: int) -> str:
        return f"{RAW}dt={day}/{day.replace('-', '')}T{(i // 60) % 24:02d}{i % 60:02d}-{i:05d}.parquet"

    def write(self, day: str, numbers, rows) -> list[str]:
        from land_parquet import parquet_bytes
        bodies = {self.key(day, i): parquet_bytes(rows(day, i)) for i in numbers}
        put_all(self.s3, self.bucket, bodies)
        return sorted(bodies)

    def land(self, day: str, n: int, start: int = 0, register: bool = True) -> list[str]:
        """n more files of 12 rows under the day's raw folder (the late file of SIM05)."""
        keys = self.write(day, range(start, start + n), twelve_rows)
        self.s3.delete_object(Bucket=self.bucket, Key=f"{MARKS}dt={day}.json")     # the folder is not the set now
        if register:
            self.cd.register(self.glue, self.db, self.raw, self.bucket, [day])
        return keys

    # -- the night, and what it left ------------------------------------------------------------------------
    def night(self, now: datetime | None = None, context=None) -> dict | None:
        now = now or self.night_at
        try:
            item = self.cd.nightly(now, self.cfg(), s3=self.s3, glue=self.glue, table=self.table,
                                   athena=self.athena, log=self.printed.append, after_list=self.after_list)
        except Exception as err:                       # noqa: BLE001  the Then names the error it expects
            self.error = err
            item = self.cd.Log(self.table).get(self.cd.iso(now))
            self.printed.append(f"error: {err}")
        else:
            self.error = None
        self.items.append(item)
        return item

    def location(self, day: str) -> str | None:
        return self.cd.partition_location(self.glue, self.db, self.raw, day)

    def keys(self, prefix: str) -> list[str]:
        return sorted(self.cd.listing(self.s3, self.bucket, prefix))

    def query(self, sql: str) -> list[dict]:
        return self.athena.run(sql, self.cfg().workgroup, self.db)

    # -- SIM03: an Iceberg copy of the change log with many small files ---------------------------------------
    def iceberg_copy(self, table: str, files: dict[str, int], rows_each: int = 5) -> None:
        """`table` in the database of the lake, with the columns of trip_changes (main/sql/create_table.sql) and
        `files[day]` data files on each day, written by PyIceberg in one commit."""
        import pyarrow as pa
        from pyiceberg.io.pyarrow import _dataframe_to_data_files

        loc_prefix = f"lake/{table}/"
        try:
            self.glue.delete_table(DatabaseName=self.db, Name=table)
        except self.glue.exceptions.EntityNotFoundException:
            pass
        delete_prefix(self.s3, self.bucket, loc_prefix)
        ddl = (ROOT / "main" / "sql" / "create_table.sql").read_text()
        ddl = "\n".join(line for line in ddl.splitlines() if not line.lstrip().startswith("--"))
        self.athena.run(ddl.format(database=self.db, table=table, location=f"s3://{self.bucket}/{loc_prefix}"),
                        self.cfg().workgroup, self.db)
        tbl = self.catalog().load_table(f"{self.db}.{table}")
        schema = tbl.schema().as_arrow()
        data_files, n = [], 0
        for day, count in sorted(files.items()):
            start = datetime.fromisoformat(f"{day}T00:00:00+00:00")
            for f in range(count):
                rows = []
                for r in range(rows_each):
                    at = start + timedelta(seconds=(f * rows_each + r) * 86400 // (count * rows_each))
                    rows.append({"trip_id": f"t-{n % 400:04d}", "op": "MODIFY", "change_time": at, "arrival_time": at,
                                 "seq": str(n).rjust(40, "0"), "status": "claimed", "pickup_zone": "Downtown",
                                 "fare_quote": 10.0 + n % 17, "driver_id": f"d-{n % 30}"})
                    n += 1
                data_files += list(_dataframe_to_data_files(tbl.metadata, pa.Table.from_pylist(rows, schema=schema),
                                                            tbl.io))
        with tbl.transaction() as tx:
            with tx.update_snapshot().fast_append() as append:
                for df in data_files:
                    append.append_data_file(df)
        self.iceberg = f"{self.db}.{table}:change_time"
        self.printed.append(f"{self.db}.{table}: {len(data_files)} data files in one commit "
                            f"({', '.join(f'{d} {c}' for d, c in sorted(files.items()))})")

    def catalog(self):
        from pyiceberg.catalog.glue import GlueCatalog
        c = self.sess.get_credentials().get_frozen_credentials()
        keys = {"access-key-id": c.access_key, "secret-access-key": c.secret_key, "session-token": c.token,
                "region": REGION}
        props = {f"client.{k}": v for k, v in keys.items() if v} | {f"s3.{k}": v for k, v in keys.items() if v}
        return GlueCatalog("glue", **props)

    def iceberg_files(self, table: str) -> dict[str, int]:
        return {r["day"]: int(r["files"]) for r in self.query(self.cd.files_sql(self.db, table, "change_time"))}


def twelve_rows(day: str, i: int) -> list[dict]:
    """File i of a day of SIM02 or SIM05: 12 of SIM01's deliveries, with ids of their own, received on that day."""
    t = template()
    out = []
    for j in range(ROWS_PER_FILE):
        d = t[(i * ROWS_PER_FILE + j) % len(t)]
        stamp = f"{day}T{(i // 60) % 24:02d}:{i % 60:02d}:{j * 4:02d}.000Z"
        tag = f"{day.replace('-', '')}-{i:05d}-{j:02d}"
        out.append({**d, "event_id": f"{tag}-{d['event_id']}", "trip_id": f"{tag}-{d['trip_id']}",
                    "delivery_id": f"{tag}-{d['delivery_id']}", "received_time": stamp})
    return out


def thirty_days(today: date):
    """SIM01's hour on each of the 30 days before `today`, one file a minute: what land_parquet.py --days 30 lands
    on that day. Returns (plan, rows)."""
    from land_parquet import slots, spread
    groups = slots(spread(template(), 30, 1440, today), 1440)
    plan: dict[str, int] = {}
    for day, _ in groups:
        plan[day] = plan.get(day, 0) + 1
    assert all(n == 1440 for n in plan.values()), plan
    return plan, lambda day, i: groups[(day, i)]


def capture(context, scenario) -> None:
    """Append the example's lines (and Athena's Data scanned of each statement) to output/behave-live.txt."""
    lake = getattr(context, "lake", None)
    lines = [f"--- {scenario.name} ({scenario.status.name}) {datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}"]
    if lake is not None:
        lines += lake.printed
        lines += [f"  athena {x['state']:<9} {x['scanned']:>10} B {x['ms']:>7} ms  {x['sql']}" for x in lake.athena.spent]
    if context.printed:
        lines += ["  printed: " + line for line in context.printed.splitlines()]
    account = os.environ.get("LIVE_ACCOUNT") or session().client("sts").get_caller_identity()["Account"]
    os.environ["LIVE_ACCOUNT"] = account
    with open(ROOT / "output" / "behave-live.txt", "a") as f:
        f.write("\n".join(line.replace(account, MASK) for line in lines) + "\n")


# ---- down --------------------------------------------------------------------------------------------------

def athena_spent(sess, workgroups: list[str]) -> list[str]:
    """Every query of the workgroups, from ListQueryExecutions and GetQueryExecution: Data scanned and its USD."""
    athena = sess.client("athena")
    lines, total, n = [], 0, 0
    for wg in workgroups:
        ids = []
        try:
            for page in athena.get_paginator("list_query_executions").paginate(WorkGroup=wg):
                ids += page["QueryExecutionIds"]
        except athena.exceptions.InvalidRequestException:
            continue
        scanned, billed = 0, 0
        for i in range(0, len(ids), 50):
            for q in athena.batch_get_query_execution(QueryExecutionIds=ids[i:i + 50])["QueryExecutions"]:
                b = q.get("Statistics", {}).get("DataScannedInBytes", 0)
                scanned += b
                # failed queries and DDL that scans nothing are not billed; the others (a CTAS and OPTIMIZE are
                # reported as DDL) at their Data scanned, 10 MB at least
                if q["Status"]["State"] != "FAILED" and (q.get("StatementType") != "DDL" or b > 0):
                    billed += max(b, 10 * 2 ** 20)
        total += billed
        n += len(ids)
        lines.append(f"{wg}: {len(ids)} queries, {scanned} B Data scanned, {billed} B billed (10 MB minimum a query) "
                     f"= {billed / 2 ** 40 * 5.50:.4f} USD")
    lines.append(f"Athena in all: {n} queries, {total / 2 ** 40 * 5.50:.4f} USD ({total / 2 ** 40 * 5.50 * 1.45:.4f} CAD)")
    return lines


def down(yes: bool, log=print) -> int:
    """Everything the @live examples made under $PREFIX-live: the Glue database and its tables, every object of
    the bucket, the run log, the workgroups, the bucket. Without yes, only the list."""
    import lake
    import run_log
    from tags import record, recorded
    use = os.environ["PREFIX"]
    sess = session()
    s3, glue, ddb = sess.client("s3"), sess.client("glue"), sess.client("dynamodb")
    bucket, account = lake.bucket_name(sess), lake.account_id(sess)
    db = f"{use.replace('-', '_')}_lake"
    verb = "removed " if yes else "would remove"
    say = lambda text: log(text.replace(account, MASK))      # noqa: E731
    if yes:
        out = ROOT / "output" / "behave-live-cost.txt"
        out.write_text("\n".join(athena_spent(sess, [f"{use}-wg", f"{use}-ctas-wg"])) + "\n")
        say(f"wrote    {out.relative_to(ROOT)}")
    try:
        tables = [t["Name"] for t in glue.get_tables(DatabaseName=db)["TableList"]]
        for t in tables:
            if yes:
                glue.delete_table(DatabaseName=db, Name=t)
            say(f"{verb} Glue table {db}.{t}")
        if yes:
            glue.delete_database(Name=db)
            record("glue-database", db, "deleted", script="features/live.py down")
        say(f"{verb} Glue database {db}")
    except glue.exceptions.EntityNotFoundException:
        say(f"absent   Glue database {db}")
    try:
        keys = sum((len(p.get("Contents", [])) for p in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket)), 0)
        if yes:
            delete_prefix(s3, bucket, "")
        say(f"{verb} {keys} objects of s3://{bucket} (raw/, compacted/, lake/, maintenance/, live-fixtures/, athena-results/)")
    except s3.exceptions.NoSuchBucket:
        say(f"absent   s3://{bucket}")
    table = run_log.run_log_name()
    if recorded("dynamodb-table", table) == "created":
        try:
            if yes:
                ddb.delete_table(TableName=table)
                record("dynamodb-table", table, "deleted", script="features/live.py down")
            say(f"{verb} run log {table}")
        except ddb.exceptions.ResourceNotFoundException:
            say(f"absent   run log {table}")
    else:
        say(f"kept     run log {table} ({recorded('dynamodb-table', table) or 'not in the ledger'})")
    code = lake.down(sess, yes=yes, log=say)
    return code


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("down")
    d.add_argument("--yes", action="store_true")
    args = p.parse_args(argv)
    REAL_ENV.update(os.environ)
    use_live_env()
    code = down(args.yes)
    if not args.yes:
        print("Nothing deleted. Run again with --yes to delete these.")
    return code


if __name__ == "__main__":
    sys.exit(main())

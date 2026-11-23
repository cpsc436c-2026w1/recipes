"""Remove what ext-scheduled-merge created for one $PREFIX in ca-central-1, then prove it is gone.

    PREFIX=recipe-yourname uv run python ext-scheduled-merge/clean_up.py           # what it would delete
    PREFIX=recipe-yourname uv run python ext-scheduled-merge/clean_up.py --yes     # delete it
    PREFIX=recipe-yourname uv run python ext-scheduled-merge/clean_up.py --check   # what is left: all empty?
    PREFIX=recipe-yourname uv run python scripts/lake.py down --yes                # last: the bucket and workgroups

In this order: the schedule group (deleting it deletes the hourly schedule, so no run starts while the rest goes),
the function and its log group, the two roles the ledger records `created`, the run log's items of job `merge`
(and the table, only when the ledger records it `created` and no other job has items in it), those of the Glue
tables `trips`, `zone_counts` and `trip_events` that the ledger records `created` by `merge.py up` and then the
database `<prefix>_lake` when the ledger records it `created` and it is empty (tables made by Athena DDL carry no
tags, so the ledger is what says they are ours; a table that was there before `up` is `borrowed` and kept), the
prefixes `lake/merge-state/` and `quarantine/trip-events/` of the lake bucket, and `lake/trips/` and
`lake/zone_counts/` only when their table is ours. It never deletes the bucket, `raw/trip-events/` or the shared
workgroups `<PREFIX>-wg` and `<PREFIX>-ctas-wg`.

Every step is attempted, also after one fails; with a failed step the exit code is 1. Running it twice is safe.
"""
from __future__ import annotations

import argparse
import sys
from contextlib import contextmanager
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError

HERE = Path(__file__).resolve().parent
# Appended, not put first: a folder of this recipe never hides a module of the same name in another one
# (each extension has its own clean_up.py).
sys.path += [p for p in [str(HERE), str(HERE.parent / "scripts")] if p not in sys.path]
from tags import prefix, record, recorded  # noqa: E402

REGION = "ca-central-1"
SCRIPT = "ext-scheduled-merge/clean_up.py --yes"
PREFIXES = ("lake/trips/", "lake/zone_counts/", "lake/merge-state/", "quarantine/trip-events/")
TABLES = ("trips", "zone_counts", "trip_events")
TABLE_OF_PREFIX = {"lake/trips/": "trips", "lake/zone_counts/": "zone_counts"}   # data of a table: kept with it
JOB = "merge"
GONE = {"ResourceNotFoundException", "ResourceNotFound", "NotFoundException", "NotFound", "404", "NoSuchEntity",
        "NoSuchBucket", "EntityNotFoundException"}


def names() -> dict:
    p = prefix()
    return {"function": f"{p}-merge-run", "roles": (f"{p}-merge-role", f"{p}-merge-scheduler-role"),
            "group": f"{p}-scheduled-merge", "log_group": f"/aws/lambda/{p}-merge-run", "run_log": f"{p}-runs",
            "database": f"{p.replace('-', '_')}_lake"}


class Steps:
    """removed (or would be, without --yes), absent, kept (not ours to delete), failed."""

    def __init__(self, yes: bool):
        self.yes = yes
        self.lines: list[tuple[str, str]] = []

    def note(self, kind: str, what: str) -> None:
        self.lines.append((kind, what))

    def do(self, what: str, fn=None, **kw) -> bool:
        try:
            if fn and self.yes:
                fn(**kw)
        except (ClientError, BotoCoreError) as err:
            code = getattr(err, "response", {}).get("Error", {}).get("Code", "")
            self.note("absent" if code in GONE else "FAILED", what if code in GONE else f"{what}: {code}: {err}")
            return False
        self.note("removed" if self.yes else "would remove", what)
        return True

    @contextmanager
    def part(self, what: str):
        try:
            yield
        except (ClientError, BotoCoreError) as err:
            code = getattr(err, "response", {}).get("Error", {}).get("Code", "")
            self.note("absent" if code in GONE else "FAILED", what if code in GONE else f"{what}: {code}: {err}")

    def report(self, log=print) -> int:
        for kind, what in self.lines:
            log(f"{kind:<12} {what}")
        return 1 if any(k == "FAILED" for k, _ in self.lines) else 0


def bucket(sess) -> str:
    return f"{prefix()}-lake-{sess.client('sts').get_caller_identity()['Account']}"


def keys_under(s3, name: str, pfx: str) -> list[str]:
    out = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=name, Prefix=pfx):
        out += [o["Key"] for o in page.get("Contents", [])]
    return out


def table_is_ours(database: str, table: str, states=("created",)) -> bool:
    """A Glue table carries no tags, so the ledger decides: only a table `merge.py up` recorded `created`."""
    return recorded("glue-table", f"{database}.{table}") in states


def prefix_is_ours(database: str, pfx: str) -> bool:
    """merge-state/ and quarantine/ only this extension writes; a table's data goes only with an own table (also
    after the table itself is deleted, so a second run finishes a first one that failed half-way)."""
    table = TABLE_OF_PREFIX.get(pfx)
    return table is None or table_is_ours(database, table, ("created", "deleted"))


def job_items(table) -> list[dict]:
    from boto3.dynamodb.conditions import Key
    items, kw = [], {}
    while True:
        page = table.query(KeyConditionExpression=Key("job").eq(JOB), ProjectionExpression="job, run_id", **kw)
        items += page["Items"]
        if "LastEvaluatedKey" not in page:
            return items
        kw = {"ExclusiveStartKey": page["LastEvaluatedKey"]}


def clean(sess=None, yes: bool = False, log=print) -> int:
    sess = sess or boto3.session.Session(region_name=REGION)
    n, steps = names(), Steps(yes)
    sch, lam, logs, iam = (sess.client(c) for c in ("scheduler", "lambda", "logs", "iam"))
    groups = [g["Name"] for g in sch.list_schedule_groups(NamePrefix=n["group"])["ScheduleGroups"]]
    if n["group"] not in groups:                     # looked up first: a missing group is simply absent
        steps.note("absent", f"schedule group {n['group']}")
    elif steps.do(f"schedule group {n['group']} and its schedules", sch.delete_schedule_group, Name=n["group"]) \
            and yes:
        record("schedule-group", n["group"], "deleted", script=SCRIPT)
    if steps.do(f"function {n['function']}", lam.delete_function, FunctionName=n["function"]) and yes:
        record("lambda-function", n["function"], "deleted", script=SCRIPT)
    if steps.do(f"log group {n['log_group']}", logs.delete_log_group, logGroupName=n["log_group"]) and yes:
        record("log-group", n["log_group"], "deleted", script=SCRIPT)
    for role in n["roles"]:
        state = recorded("iam-role", role)
        if state != "created":
            steps.note("kept", f"role {role} ({state or 'not in the ledger'})")
            continue
        with steps.part(f"role {role}"):
            for policy in iam.list_role_policies(RoleName=role)["PolicyNames"]:
                if yes:
                    iam.delete_role_policy(RoleName=role, PolicyName=policy)
            if steps.do(f"role {role}", iam.delete_role, RoleName=role) and yes:
                record("iam-role", role, "deleted", script=SCRIPT)
    with steps.part(f"run log {n['run_log']}, items of job {JOB}"):
        table = sess.resource("dynamodb").Table(n["run_log"])
        items = job_items(table)
        if yes:
            with table.batch_writer() as batch:
                for item in items:
                    batch.delete_item(Key={"job": item["job"], "run_id": item["run_id"]})
        steps.note("removed" if yes else "would remove", f"{len(items)} items of job {JOB} in {n['run_log']}")
        others = table.scan(Limit=25, ProjectionExpression="job")["Items"]
        others = [i for i in others if i["job"] != JOB]
        if recorded("dynamodb-table", n["run_log"]) != "created":
            steps.note("kept", f"table {n['run_log']} ({recorded('dynamodb-table', n['run_log']) or 'not in the ledger'})")
        elif others:
            steps.note("kept", f"table {n['run_log']} (job {others[0]['job']} has items)")
        elif steps.do(f"table {n['run_log']}", sess.client("dynamodb").delete_table, TableName=n["run_log"]) and yes:
            record("dynamodb-table", n["run_log"], "deleted", script=SCRIPT)
    glue = sess.client("glue")
    for t in TABLES:
        name = f"{n['database']}.{t}"
        if not table_is_ours(n["database"], t):
            steps.note("kept", f"Glue table {name} ({recorded('glue-table', name) or 'not in the ledger'})")
            continue
        if steps.do(f"Glue table {name}", glue.delete_table, DatabaseName=n["database"], Name=t) and yes:
            record("glue-table", name, "deleted", script=SCRIPT)
    with steps.part(f"Glue database {n['database']}"):
        state = recorded("glue-database", n["database"])
        left = [t["Name"] for t in glue.get_tables(DatabaseName=n["database"])["TableList"]]
        if state != "created":
            steps.note("kept", f"Glue database {n['database']} ({state or 'not in the ledger'})")
        elif yes and left:
            steps.note("kept", f"Glue database {n['database']} (tables left: {', '.join(left)})")
        elif steps.do(f"Glue database {n['database']}", glue.delete_database, Name=n["database"]) and yes:
            record("glue-database", n["database"], "deleted", script=SCRIPT)
    s3 = sess.client("s3")
    with steps.part("lake bucket prefixes"):
        name = bucket(sess)
        for pfx in PREFIXES:
            if not prefix_is_ours(n["database"], pfx):
                steps.note("kept", f"s3://{name}/{pfx} (data of the table {TABLE_OF_PREFIX[pfx]}, not ours)")
                continue
            keys = keys_under(s3, name, pfx)
            if yes:
                for i in range(0, len(keys), 1000):
                    s3.delete_objects(Bucket=name, Delete={"Objects": [{"Key": k} for k in keys[i:i + 1000]],
                                                           "Quiet": True})
            steps.note("removed" if yes else "would remove", f"{len(keys)} objects under s3://{name}/{pfx}")
    return steps.report(log)


def check(sess=None, log=print) -> int:
    """List what is left of this extension, by schedule group, by name and by prefix: 0 when all are empty."""
    sess = sess or boto3.session.Session(region_name=REGION)
    n, left = names(), []
    groups = [g["Name"] for g in sess.client("scheduler").list_schedule_groups(NamePrefix=n["group"])["ScheduleGroups"]]
    left += [f"schedule group {g}" for g in groups if g == n["group"]]
    fns = [f["FunctionName"] for f in sess.client("lambda").list_functions()["Functions"]]
    left += [f"function {f}" for f in fns if f == n["function"]]
    lgs = sess.client("logs").describe_log_groups(logGroupNamePrefix=n["log_group"])["logGroups"]
    left += [f"log group {g['logGroupName']}" for g in lgs]
    roles = [r["RoleName"] for r in sess.client("iam").list_roles()["Roles"]]
    left += [f"role {r}" for r in roles if r in n["roles"] and recorded("iam-role", r) in ("created", "deleted")]
    try:
        tables = [t["Name"] for t in sess.client("glue").get_tables(DatabaseName=n["database"])["TableList"]]
        left += [f"Glue table {n['database']}.{t}" for t in tables
                 if t in TABLES and table_is_ours(n["database"], t, ("created", "deleted"))]
    except ClientError:
        pass
    try:
        left += [f"run log items of job {JOB}: {len(job_items(sess.resource('dynamodb').Table(n['run_log'])))}"
                 ] if job_items(sess.resource("dynamodb").Table(n["run_log"])) else []
    except ClientError:
        pass
    s3, name = sess.client("s3"), bucket(sess)
    for pfx in PREFIXES:
        if not prefix_is_ours(n["database"], pfx):
            continue
        try:
            if keys_under(s3, name, pfx):
                left.append(f"objects under s3://{name}/{pfx}")
        except ClientError:
            pass
    for line in left:
        log(f"LEFT   {line}")
    log("nothing of ext-scheduled-merge is left" if not left else f"{len(left)} kinds of resource left")
    return 1 if left else 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--yes", action="store_true", help="delete; without it, only list what would go")
    p.add_argument("--check", action="store_true", help="list what is left, by group, name and prefix")
    args = p.parse_args(argv)
    if args.check:
        return check()
    code = clean(yes=args.yes)
    if not args.yes:
        print("Nothing deleted. Run again with --yes to delete these.")
    return code


if __name__ == "__main__":
    sys.exit(main())

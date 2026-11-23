"""ext-scheduled-merge: deploy the hourly merge, run it once, rebuild a period by hand, or play it offline.

    uv run python ext-scheduled-merge/merge.py                          offline: SIM01 into moto S3, DuckDB merges
    uv run python ext-scheduled-merge/merge.py --target aws up          tables, workgroup, run log, function, schedule
    uv run python ext-scheduled-merge/merge.py --target aws run-now     one run now (the function, synchronously)
    uv run python ext-scheduled-merge/merge.py --target aws rebuild --period 2026-11-18   one period's counts
    uv run python ext-scheduled-merge/merge.py --target aws status      the last runs in the run log
    uv run python ext-scheduled-merge/merge.py --target aws pause       the hourly schedule off; nothing deleted
    uv run python ext-scheduled-merge/merge.py --target aws resume      the hourly schedule on again

`up` makes, in ca-central-1 and with $PREFIX: the lake bucket if it is missing (scripts/lake.py up), the workgroup
$PREFIX-wg (ensure_workgroup), the run log $PREFIX-runs (ensure_run_log), the Glue database ${prefix}_lake with
trip_events, trips and zone_counts (Athena DDL, sql/athena_tables.sql), the roles $PREFIX-merge-role and
$PREFIX-merge-scheduler-role, the log group, the function $PREFIX-merge-run and the schedule group
$PREFIX-scheduled-merge with the schedule $PREFIX-merge-hourly. `clean_up.py --yes` (this folder) removes them.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
# Appended, not put first: a folder of this recipe never hides a module of the same name in another one
# (each extension has its own clean_up.py).
sys.path += [p for p in [str(HERE), str(ROOT / "scripts"), str(ROOT / "sim"), str(ROOT)] if p not in sys.path]

import merge_run  # noqa: E402
from tags import prefix, record, recorded  # noqa: E402

REGION = "ca-central-1"
SCRIPT = "ext-scheduled-merge/merge.py up"
CLEAN = "ext-scheduled-merge/clean_up.py --yes"
TIMEOUT_S, MEMORY_MB = 600, 256           # ours: 10 min > a MERGE's wait; 256 MB is plenty for the listing


def names() -> dict:
    p = prefix()
    return {"function": f"{p}-merge-run", "role": f"{p}-merge-role", "scheduler_role": f"{p}-merge-scheduler-role",
            "group": f"{p}-scheduled-merge", "schedule": f"{p}-merge-hourly", "log_group": f"/aws/lambda/{p}-merge-run",
            "workgroup": f"{p}-wg", "run_log": f"{p}-runs", "database": f"{p.replace('-', '_')}_lake"}


def ddl_statements(database: str, bucket: str) -> list[str]:
    text = (HERE / "sql" / "athena_tables.sql").read_text().format(database=database, bucket=bucket)
    parts = [p for p in text.split("\n;\n")]
    return ["\n".join(line for line in p.splitlines() if not line.lstrip().startswith("--")).strip() for p in parts]


def function_policy(account: str, bucket: str) -> dict:
    """What the merge function may do: Athena in its workgroup, Glue on its database, S3 on the lake's prefixes
    it reads and writes, the run log, and its own log group."""
    n, region = names(), REGION
    db = n["database"]
    return {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": ["athena:StartQueryExecution", "athena:GetQueryExecution",
                                       "athena:GetQueryResults", "athena:StopQueryExecution"],
         "Resource": f"arn:aws:athena:{region}:{account}:workgroup/{n['workgroup']}"},
        {"Effect": "Allow", "Action": ["glue:GetDatabase", "glue:GetTable", "glue:GetTables", "glue:GetPartitions",
                                       "glue:UpdateTable"],
         "Resource": [f"arn:aws:glue:{region}:{account}:catalog", f"arn:aws:glue:{region}:{account}:database/{db}",
                      f"arn:aws:glue:{region}:{account}:table/{db}/*"]},
        {"Effect": "Allow", "Action": ["s3:ListBucket", "s3:GetBucketLocation"], "Resource": f"arn:aws:s3:::{bucket}"},
        {"Effect": "Allow", "Action": ["s3:GetObject"], "Resource": [f"arn:aws:s3:::{bucket}/raw/trip-events/*"]},
        {"Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload"],
         "Resource": [f"arn:aws:s3:::{bucket}/{p}*" for p in ("lake/trips/", "lake/zone_counts/", "lake/merge-state/",
                                                               "quarantine/trip-events/", "athena-results/")]},
        {"Effect": "Allow", "Action": ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem",
                                       "dynamodb:DeleteItem", "dynamodb:Query"],
         "Resource": f"arn:aws:dynamodb:{region}:{account}:table/{n['run_log']}"},
        {"Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
         "Resource": f"arn:aws:logs:{region}:{account}:log-group:{n['log_group']}:*"},
    ]}


def scheduler_policy(function_arn: str) -> dict:
    return {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": "lambda:InvokeFunction", "Resource": function_arn}]}


def package(deps: bool = True) -> bytes:
    """The function's zip: merge_run.py, its SQL, the event models, and Pydantic built for arm64 (with deps)."""
    from main.jobs import zip_bytes
    files = {"merge_run.py": HERE / "merge_run.py",
             "rides/__init__.py": ROOT / "src" / "rides" / "__init__.py",
             "rides/events.py": ROOT / "src" / "rides" / "events.py"}
    files |= {f"sql/{p.name}": p for p in sorted((HERE / "sql").glob("*.sql"))}
    return zip_bytes(files, ["pydantic>=2.9"] if deps else None)


def function_env(bucket: str) -> dict:
    env = {"PREFIX": prefix(), "BUCKET": bucket}
    for k in ("K_DAYS", "PERIOD", "MAX_OBJECTS", "MERGE_FROM", "SETTLE_S"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


def schedule_definition(function_arn: str, role_arn: str) -> dict:
    """Hourly; the run's time is the scheduled time (a context attribute of the Scheduler); no retries: a failed
    hour is merged again by the next hour's run."""
    n = names()
    return {"Name": n["schedule"], "GroupName": n["group"], "ScheduleExpression": "rate(1 hour)",
            "FlexibleTimeWindow": {"Mode": "OFF"}, "State": "ENABLED",
            "Target": {"Arn": function_arn, "RoleArn": role_arn,
                       "Input": json.dumps({"time": "<aws.scheduler.scheduled-time>"}),
                       "RetryPolicy": {"MaximumRetryAttempts": 0}}}


def athena_run(athena, sql: str, workgroup: str, sleep=time.sleep) -> dict:
    qid = athena.start_query_execution(QueryString=sql, WorkGroup=workgroup)["QueryExecutionId"]
    while True:
        q = athena.get_query_execution(QueryExecutionId=qid)["QueryExecution"]
        if q["Status"]["State"] in ("SUCCEEDED", "FAILED", "CANCELLED"):
            break
        sleep(1)
    if q["Status"]["State"] != "SUCCEEDED":
        raise RuntimeError(f"Athena {q['Status']['State']}: {q['Status'].get('StateChangeReason')}\n{sql[:300]}")
    return q


def up(sess=None, deps: bool = True, log=print, sleep=time.sleep) -> dict:
    import boto3
    import lake
    import run_log
    from main import jobs

    sess = sess or boto3.session.Session(region_name=REGION)
    n = names()
    lake.up(sess, log=log)
    bucket, account = lake.bucket_name(sess), lake.account_id(sess)
    log(f"{lake.ensure_workgroup(n['workgroup'], sess=sess):<8} Athena workgroup {n['workgroup']}")
    run_log.ensure_run_log(sess)
    log(f"ready    run log {n['run_log']}")
    glue, athena = sess.client("glue"), sess.client("athena")
    before = set()
    try:
        before = {t["Name"] for t in glue.get_tables(DatabaseName=n["database"])["TableList"]}
        db_was = True
    except glue.exceptions.EntityNotFoundException:
        db_was = False
    for sql in ddl_statements(n["database"], bucket):
        athena_run(athena, sql, n["workgroup"], sleep)
    if not db_was:
        record("glue-database", n["database"], "created", script=SCRIPT, clean=CLEAN)
    for table in ("trip_events", "trips", "zone_counts"):
        if table not in before:
            record("glue-table", f"{n['database']}.{table}", "created", script=SCRIPT, clean=CLEAN)
        elif recorded("glue-table", f"{n['database']}.{table}") is None:
            record("glue-table", f"{n['database']}.{table}", "borrowed", script=SCRIPT)
    log(f"ready    Glue database {n['database']}: trip_events (projection), trips and zone_counts (Iceberg)")
    iam = sess.client("iam")
    role = jobs.ensure_role(iam, n["role"], jobs.LAMBDA_TRUST, function_policy(account, bucket), SCRIPT, CLEAN)
    jobs.ensure_log_group(sess.client("logs"), n["log_group"], SCRIPT, CLEAN)
    fn = jobs.ensure_function(sess.client("lambda"), n["function"], role, "merge_run.handler", package(deps),
                              function_env(bucket), TIMEOUT_S, MEMORY_MB, SCRIPT, CLEAN, sleep=sleep)
    log(f"ready    function {n['function']} ({TIMEOUT_S} s, {MEMORY_MB} MB, asynchronous retries 0)")
    sch_role = jobs.ensure_role(iam, n["scheduler_role"], jobs.SCHEDULER_TRUST, scheduler_policy(fn), SCRIPT, CLEAN)
    sch = sess.client("scheduler")
    jobs.ensure_schedule_group(sch, n["group"], SCRIPT, CLEAN)
    for attempt in range(10):              # the scheduler role, like any new role, takes seconds to be usable
        try:
            state = jobs.ensure_schedule(sch, schedule_definition(fn, sch_role))
            break
        except sch.exceptions.ValidationException:
            if attempt == 9:
                raise
            sleep(6)
    log(f"{state:<8} schedule {n['group']}/{n['schedule']}: rate(1 hour), retries 0")
    return {"function_arn": fn, "bucket": bucket}


def run_now(sess=None, log=print) -> dict:
    import boto3
    sess = sess or boto3.session.Session(region_name=REGION)
    from botocore.config import Config as BotoConfig
    lam = sess.client("lambda", config=BotoConfig(read_timeout=TIMEOUT_S + 30))
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = lam.invoke(FunctionName=names()["function"], Payload=json.dumps({"time": now}).encode())
    body = json.loads(out["Payload"].read() or b"{}")
    log(json.dumps(body, indent=2, default=str))
    return body


def rebuild(period: str, sess=None, log=print) -> dict:
    """The rebuild statement of R90 for one period (a day, or the week or month that starts on it)."""
    import boto3
    sess = sess or boto3.session.Session(region_name=REGION)
    cfg = merge_run.Config()
    first = merge_run.period_start(date.fromisoformat(period), cfg.period)
    end = merge_run.next_period(first, cfg.period)
    engine = merge_run.AthenaEngine(sess.client("athena"), cfg)
    qid = engine.start(merge_run.rebuild_sql(engine, first, end, cfg.period), "rebuild")
    engine.wait(qid, "rebuild")
    log(f"rebuilt  zone_counts from {first} to {end} (one {cfg.period} a row): query {qid}, "
        f"{engine.scanned[qid]} B Data scanned")
    return {"query_id": qid, "data_scanned": engine.scanned[qid]}


def status(sess=None, log=print) -> list[dict]:
    import boto3
    sess = sess or boto3.session.Session(region_name=REGION)
    items = merge_run.RunLog(sess.resource("dynamodb").Table(names()["run_log"])).runs(limit=10)
    for i in items:
        log(f"{i['run_id']}  {i['run_state']:<9} objects={i.get('objects', '-')} quarantined="
            f"{i.get('quarantined', '-')} old_events={i.get('old_events', '-')} {i.get('failed_step', '')}")
    return items


def set_state(state: str, sess=None, log=print) -> str:
    """pause (DISABLED) or resume (ENABLED) the hourly schedule. UpdateSchedule replaces the whole definition, so the
    schedule's own fields are sent back unchanged with the new state; nothing else is touched."""
    import boto3
    sess = sess or boto3.session.Session(region_name=REGION)
    n, sch = names(), sess.client("scheduler")
    s = sch.get_schedule(Name=n["schedule"], GroupName=n["group"])
    keep = {k: s[k] for k in ("ScheduleExpression", "Target", "FlexibleTimeWindow")}
    for k in ("ScheduleExpressionTimezone", "Description", "StartDate", "EndDate", "KmsKeyArn",
              "ActionAfterCompletion"):
        if s.get(k):
            keep[k] = s[k]
    sch.update_schedule(Name=n["schedule"], GroupName=n["group"], State=state, **keep)
    log(f"{state:<8} schedule {n['group']}/{n['schedule']} ({s['ScheduleExpression']})")
    return state


def local(log=print) -> int:
    """Offline: SIM01 through the replayer's s3 target into moto, two hourly runs and a repeat, DuckDB merges."""
    from main.config import offline_env
    offline_env()
    os.environ.setdefault("LEDGER", str(Path(tempfile.mkdtemp()) / "ledger.jsonl"))
    os.environ["PREFIX"] = os.environ.get("PREFIX") or "recipe"
    from moto import mock_aws

    with mock_aws():
        import boto3
        import lake
        import replay
        import run_log
        import sim_data

        sess = boto3.session.Session(region_name=REGION)
        lake.up(sess, log=lambda *_: None)
        run_log.ensure_run_log(sess)
        deliveries, truth = sim_data.load("SIM01")
        store = merge_run.Store(sess.client("s3"), lake.bucket_name(sess))
        sender = replay.S3Sender(store.s3, store.bucket, "local")
        replay.replay(deliveries, sender, "local")
        for key in sender.keys:
            store.stamps[key] = sim_data.slot_end(key)
        table = sess.resource("dynamodb").Table(run_log.run_log_name())
        engine = merge_run.DuckEngine()
        log(f"[offline: moto S3 and DynamoDB, DuckDB in place of Athena; SIM01 rush-hour, {len(deliveries)} "
            f"deliveries in {len(sender.keys)} objects]")
        for hhmm in ("10:00", "11:00", "12:00"):
            item = merge_run.run(datetime.fromisoformat(f"2026-11-23T{hhmm}:00+00:00"), store=store, table=table,
                                 engine=engine, log=lambda *_: None)
            log(f"run {hhmm}  {item['run_state']:<9} objects {item['objects']:>3}  quarantined {item['quarantined']}"
                f"  old_events {item['old_events']}")
        before = engine.rows("SELECT * FROM trips ORDER BY trip_id")
        # The repeat: every object merged a second time, as the run after a failed rebuild does (R100).
        objects = [merge_run.Obj(k, "2026-11-23", m) for k, m in store.list("raw/trip-events/")]
        for o in objects:
            merge_run.check(store, o)
        engine.start(merge_run.merge_sql(engine, objects, store), "merge")
        same = engine.rows("SELECT * FROM trips ORDER BY trip_id") == before
        zones = dict(engine.rows("SELECT pickup_zone_id, count(*) FROM trips GROUP BY 1"))
        counts = engine.rows("SELECT pickup_zone_id, trips_requested, trips_completed FROM zone_counts "
                             "WHERE period_start = DATE '2026-11-23' ORDER BY 1")
        want = truth["requested_per_zone"]
        log(f"trips: {sum(zones.values())} rows; zones 48, 161, 230: {zones[48]}, {zones[161]}, {zones[230]} "
            f"(truth: {want['48']}, {want['161']}, {want['230']})")
        log("zone_counts 2026-11-23 (zone, requested, completed): "
            + ", ".join(f"{z} {r} {c}" for z, r, c in counts))
        log(f"the same objects merged again leave trips unchanged: {same}")
        ok = same and [zones[48], zones[161], zones[230]] == [want["48"], want["161"], want["230"]]
        return 0 if ok else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--target", choices=["local", "aws"], default="local")
    p.add_argument("command", nargs="?", choices=["up", "run-now", "rebuild", "status", "pause", "resume"],
                   help="with --target aws")
    p.add_argument("--period", help="rebuild: the first day of the period (YYYY-MM-DD)")
    args = p.parse_args(argv)
    if args.target == "local":
        return local()
    if args.command == "up":
        up()
    elif args.command == "run-now":
        return 0 if run_now().get("run_state") in ("finished", "skipped") else 1
    elif args.command == "rebuild":
        if not args.period:
            p.error("rebuild needs --period YYYY-MM-DD")
        rebuild(args.period)
    elif args.command == "status":
        status()
    elif args.command in ("pause", "resume"):
        set_state("DISABLED" if args.command == "pause" else "ENABLED")
    else:
        p.error("--target aws needs a command: up, run-now, rebuild, status, pause or resume")
    return 0


if __name__ == "__main__":
    sys.exit(main())

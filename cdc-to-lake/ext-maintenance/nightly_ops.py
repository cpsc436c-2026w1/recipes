"""The terminal side of the nightly compaction (maintenance.py --target aws ...): deploy it, switch the raw table to
registered partitions, and measure the 30-day count before and after.

    deploy_cli(email)       --schedule [--email]: the raw table if missing, workgroups, run log, roles, function
                            <PREFIX>-compact-days, schedule group <PREFIX>-maintenance-compaction with its two
                            schedules, topic <PREFIX>-maintenance-alerts and alarms <PREFIX>-compact-errors and
                            <PREFIX>-compact-missed (R113, R141)
    register_partitions()   --register-partitions: projection off, every day folder and the next 7 days (R131)
    measure()               --measure [--all-days]: a spend estimate first, a stop over MAX_MEASURE_USD (R135)
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
# Appended, not put first: a folder of this recipe never hides a module of the same name in another one
# (each extension has its own clean_up.py).
sys.path += [p for p in [str(HERE), str(ROOT / "scripts"), str(ROOT)] if p not in sys.path]

import compact_days as cd  # noqa: E402
from tags import prefix, record, tags  # noqa: E402

REGION = "ca-central-1"
SCRIPT = "ext-maintenance/maintenance.py --target aws --schedule"
CLEAN = "ext-maintenance/clean_up.py --yes"
TIMEOUT_S, MEMORY_MB = 900, 256
GET_USD_PER_1000 = 0.00044            # S3 GET, ca-central-1, course price sheet 2026-11-18
ATHENA_USD_PER_TB = 5.50              # Athena, per TB of Data scanned, 10 MB minimum a query (price sheet)
MIN_SCAN_MB = 10
clock = lambda: datetime.now(timezone.utc)      # noqa: E731  the scenarios set the night's time
DATA_COLUMNS = [("event_type", "string"), ("event_id", "string"), ("trip_id", "string"), ("pickup_zone_id", "int"),
                ("driver_id", "string"), ("dropoff_zone_id", "int"), ("trip_miles", "double"), ("trip_time_s", "int"),
                ("fare", "double"), ("driver_pay", "double"), ("event_time", "string"), ("delivery_id", "string"),
                ("received_time", "string")]


def names() -> dict:
    p = prefix()
    return {"function": f"{p}-compact-days", "role": f"{p}-compact-role", "scheduler_role": f"{p}-compact-scheduler-role",
            "group": f"{p}-maintenance-compaction", "topic": f"{p}-maintenance-alerts",
            "errors": f"{p}-compact-errors", "missed": f"{p}-compact-missed", "log_group": f"/aws/lambda/{p}-compact-days",
            "run_log": f"{p}-runs"}


def session(sess=None):
    import boto3
    return sess or boto3.session.Session(region_name=REGION)


def lake_bucket(sess) -> str:
    import lake
    return lake.bucket_name(sess)


# ---- the raw table ----------------------------------------------------------------------------------------------

def ensure_raw_table(glue, cfg: cd.Config, bucket: str) -> str:
    """trip_events_parquet: Parquet, partitioned by dt, partitions registered (no projection). Created through the
    Glue API (the same catalog entry Athena DDL makes) if missing; an existing one is left as it is."""
    db, table = cfg.raw_table
    try:
        glue.create_database(DatabaseInput={"Name": db})
        record("glue-database", db, "created", script=SCRIPT, clean=CLEAN)
    except glue.exceptions.AlreadyExistsException:
        pass
    try:
        glue.get_table(DatabaseName=db, Name=table)
        return "exists"
    except glue.exceptions.EntityNotFoundException:
        pass
    glue.create_table(DatabaseName=db, TableInput={
        "Name": table, "TableType": "EXTERNAL_TABLE", "Parameters": {"classification": "parquet", "EXTERNAL": "TRUE"},
        "PartitionKeys": [{"Name": "dt", "Type": "string"}],
        "StorageDescriptor": {
            "Columns": [{"Name": n, "Type": t} for n, t in DATA_COLUMNS],
            "Location": f"s3://{bucket}/{cd.RAW}",
            "InputFormat": "org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat",
            "OutputFormat": "org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat",
            "SerdeInfo": {"SerializationLibrary": "org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe"}}})
    record("glue-table", f"{db}.{table}", "created", script=SCRIPT, clean="kept: the landing path may still feed it")
    return "created"


def register_partitions(now: datetime, sess=None, log=print) -> int:
    """R131: projection off, then one partition for every dt= folder under raw/trip-events/ and for the next 7 days
    (today and the 6 after it), each at its raw folder."""
    sess = session(sess)
    cfg = cd.Config(bucket=lake_bucket(sess))
    glue, s3 = sess.client("glue"), sess.client("s3")
    db, table = cfg.raw_table
    ensure_raw_table(glue, cfg, cfg.bucket)          # the README runs this before --schedule, which also makes it
    t = glue.get_table(DatabaseName=db, Name=table)["Table"]
    params = dict(t.get("Parameters", {}), **{"projection.enabled": "false"})
    keep = ("Name", "Description", "Owner", "Retention", "StorageDescriptor", "PartitionKeys", "TableType")
    glue.update_table(DatabaseName=db, TableInput={**{k: t[k] for k in keep if k in t}, "Parameters": params})
    folders = set()
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=cfg.bucket, Prefix=cd.RAW, Delimiter="/"):
        folders |= {p["Prefix"][len(cd.RAW) + 3:-1] for p in page.get("CommonPrefixes", [])}
    ahead = {(now.date() + timedelta(days=i)).isoformat() for i in range(cd.REGISTER_AHEAD)}
    added = cd.register(glue, db, table, cfg.bucket, sorted(folders | ahead))
    log(f"projection.enabled = false on {db}.{table}; registered {len(added)} partitions")
    return len(added)


def register_partitions_cli() -> int:
    register_partitions(clock())
    return 0


# ---- measure ------------------------------------------------------------------------------------------------

def estimate(files_before: int, marked_files: list[int], statements: int) -> float:
    """USD: one GET per file read (the before query; the CTAS and the row count of each marked day; the after
    query, priced as if nothing were compacted, since its files are not known before), and each statement at the
    10 MB minimum."""
    gets = files_before + 2 * sum(marked_files) + files_before
    return gets * GET_USD_PER_1000 / 1000 + statements * MIN_SCAN_MB / 2 ** 20 * ATHENA_USD_PER_TB


def count_by_zone_sql(db: str, table: str, first: str) -> str:
    return (f"SELECT pickup_zone_id, count(*) AS trips FROM {db}.{table} WHERE dt >= '{first}' "
            f"AND event_type = 'trip_requested' GROUP BY 1 ORDER BY 1")


def measure(now: datetime, all_days: bool, *, sess=None, athena=None, log=print) -> int:
    """R135: the estimate, then (under MAX_MEASURE_USD) the 30-day count, every marked day compacted, the count
    again, with Data scanned, run time and file count of both."""
    sess = session(sess)
    limit = float(os.environ.get("MAX_MEASURE_USD", "0.10"))
    cfg = cd.Config(bucket=lake_bucket(sess), compact_max_days=10 ** 6 if all_days else
                    int(os.environ.get("COMPACT_MAX_DAYS", "10")))
    s3, glue = sess.client("s3"), sess.client("glue")
    db, table = cfg.raw_table
    first = (now - timedelta(days=cd.COUNT_DAYS)).date()
    per_day = {}
    for i in range(cd.COUNT_DAYS + 1):
        day = (first + timedelta(days=i)).isoformat()
        location = cd.partition_location(glue, db, table, day) or cd.raw_folder(cfg.bucket, day)
        b, p = cd.key_of(location)
        n = len(cd.listing(s3, b, p))
        if n:
            per_day[day] = n
    marked = sorted(d for d, n in per_day.items() if n > cfg.limit and cd.closed(date.fromisoformat(d), now, cfg))
    marked = marked[:cfg.compact_max_days]
    statements = 2 + 4 * len(marked)
    usd = estimate(sum(per_day.values()), [per_day[d] for d in marked], statements)
    if usd > limit:
        log(f"estimated {usd:.2f} USD, over MAX_MEASURE_USD {limit:.2f}: {sum(per_day.values())} files over "
            f"{len(per_day)} days, {len(marked)} days to compact; nothing was run")
        return 1
    log(f"estimated {usd:.2f} USD (MAX_MEASURE_USD {limit:.2f}): {sum(per_day.values())} files, "
        f"{len(marked)} days to compact")
    athena = athena or cd.Athena(sess.client("athena"))
    sql = count_by_zone_sql(db, table, first.isoformat())
    t0 = len(athena.spent)
    before = athena.run(sql, cfg.workgroup, db)
    night = cd.Night(now, cfg, s3, glue, sess.resource("dynamodb").Table(names()["run_log"]), athena, log)
    for day in marked:
        night.copy_day(day)
    after = athena.run(sql, cfg.workgroup, db)
    files_after = 0
    for day in per_day:
        b, p = cd.key_of(cd.partition_location(glue, db, table, day) or cd.raw_folder(cfg.bucket, day))
        files_after += len(cd.listing(s3, b, p))
    spent = athena.spent[t0:]
    log(f"{'':<8}{'files':>8}{'data scanned':>16}{'run time':>11}")
    log(f"{'before':<8}{sum(per_day.values()):>8}{spent[0]['scanned']:>14} B{spent[0]['ms']:>8} ms")
    log(f"{'after':<8}{files_after:>8}{spent[-1]['scanned']:>14} B{spent[-1]['ms']:>8} ms")
    log(f"count by zone before: {json.dumps(before)}")
    log(f"count by zone after:  {json.dumps(after)}")
    log(f"same answer before and after: {before == after}")
    total = sum(x["scanned"] for x in spent)
    log(f"spent: {len(spent)} statements, {total} B Data scanned in all (failed or cancelled ones included)")
    return 0 if before == after else 1


def measure_cli(all_days: bool) -> int:
    return measure(clock(), all_days)


# ---- deploy -----------------------------------------------------------------------------------------------------

def function_policy(account: str, bucket: str, cfg: cd.Config, cdc_bucket: str) -> dict:
    n = names()
    dbs = sorted({cfg.raw_table[0]} | {db for db, _, _ in cfg.iceberg_tables})
    wgs = [f"arn:aws:athena:{REGION}:{account}:workgroup/{w}" for w in (cfg.workgroup, cfg.ctas_workgroup)]
    glue_res = [f"arn:aws:glue:{REGION}:{account}:catalog"]
    for db in dbs:
        glue_res += [f"arn:aws:glue:{REGION}:{account}:database/{db}", f"arn:aws:glue:{REGION}:{account}:table/{db}/*"]
    return {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": ["athena:StartQueryExecution", "athena:GetQueryExecution",
                                       "athena:GetQueryResults"], "Resource": wgs},
        {"Effect": "Allow", "Action": ["glue:GetDatabase", "glue:GetTable", "glue:GetTables", "glue:CreateTable",
                                       "glue:DeleteTable", "glue:UpdateTable", "glue:GetPartition",
                                       "glue:GetPartitions", "glue:UpdatePartition", "glue:BatchCreatePartition",
                                       "glue:CreatePartition"], "Resource": glue_res},
        {"Effect": "Allow", "Action": ["s3:ListBucket", "s3:GetBucketLocation"],
         "Resource": [f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{cdc_bucket}"]},
        {"Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload"],
         "Resource": [f"arn:aws:s3:::{bucket}/{p}*" for p in (cd.RAW, cd.COMPACTED, "maintenance/", "athena-results/",
                                                               "lake/")] + [f"arn:aws:s3:::{cdc_bucket}/lake/*"]},
        {"Effect": "Allow", "Action": ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem",
                                       "dynamodb:DeleteItem", "dynamodb:Query"],
         "Resource": f"arn:aws:dynamodb:{REGION}:{account}:table/{n['run_log']}"},
        {"Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
         "Resource": f"arn:aws:logs:{REGION}:{account}:log-group:{n['log_group']}:*"},
    ]}


def alarm_definitions(function: str, topic_arn: str) -> list[dict]:
    """R141: an error in a day, and a day without an invocation (missing data counts as breaching)."""
    n = names()
    common = {"Namespace": "AWS/Lambda", "Statistic": "Sum", "Period": 86400, "EvaluationPeriods": 1,
              "Dimensions": [{"Name": "FunctionName", "Value": function}], "AlarmActions": [topic_arn],
              "Tags": tags("cloudwatch")}
    return [
        {"AlarmName": n["errors"], "MetricName": "Errors", "Threshold": 1,
         "ComparisonOperator": "GreaterThanOrEqualToThreshold", "TreatMissingData": "notBreaching",
         "AlarmDescription": "the nightly compaction reported an error", **common},
        {"AlarmName": n["missed"], "MetricName": "Invocations", "Threshold": 1,
         "ComparisonOperator": "LessThanThreshold", "TreatMissingData": "breaching",
         "AlarmDescription": "the nightly compaction did not run in a day", **common},
    ]


def merge_reads_raw(sess) -> bool:
    """scheduled-merge reads the same raw files when its schedule group exists or the run log has job merge."""
    p = prefix()
    groups = [g["Name"] for g in sess.client("scheduler").list_schedule_groups(NamePrefix=f"{p}-scheduled-merge")
              .get("ScheduleGroups", [])]
    if f"{p}-scheduled-merge" in groups:
        return True
    try:
        from boto3.dynamodb.conditions import Key
        items = sess.resource("dynamodb").Table(f"{p}-runs").query(KeyConditionExpression=Key("job").eq("merge"),
                                                                    Limit=1)["Items"]
        return bool(items)
    except Exception:          # noqa: BLE001  no run log yet: no merge
        return False


def deploy(email: str | None, sess=None, log=print, sleep=time.sleep) -> dict:
    import lake
    import run_log
    from main import jobs
    from main.config import Settings

    sess = session(sess)
    n = names()
    lake.up(sess, log=log)
    bucket, account = lake.bucket_name(sess), lake.account_id(sess)
    cfg = cd.Config(bucket=bucket)
    for wg in (cfg.workgroup, cfg.ctas_workgroup):
        log(f"{lake.ensure_workgroup(wg, sess=sess):<8} Athena workgroup {wg}")
    run_log.ensure_run_log(sess)
    log(f"{ensure_raw_table(sess.client('glue'), cfg, bucket):<8} table {'.'.join(cfg.raw_table)} (registered partitions)")
    reads_raw = os.environ.get("MERGE_READS_RAW") == "1" or merge_reads_raw(sess)
    env = {"PREFIX": prefix(), "BUCKET": bucket, "MERGE_READS_RAW": "1" if reads_raw else "0"}
    for k in ("MAX_FILES_PER_QUERY", "QUERY_DAYS", "CLOSE_AFTER_HOURS", "COMPACT_MAX_DAYS", "ICEBERG_TABLES",
              "RAW_TABLE"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    iam = sess.client("iam")
    cdc_bucket = Settings().bucket(account)
    role = jobs.ensure_role(iam, n["role"], jobs.LAMBDA_TRUST, function_policy(account, bucket, cfg, cdc_bucket),
                            SCRIPT, CLEAN)
    jobs.ensure_log_group(sess.client("logs"), n["log_group"], SCRIPT, CLEAN)
    code = jobs.zip_bytes({"compact_days.py": HERE / "compact_days.py"})
    fn = jobs.ensure_function(sess.client("lambda"), n["function"], role, "compact_days.handler", code, env,
                              TIMEOUT_S, MEMORY_MB, SCRIPT, CLEAN, sleep=sleep)
    log(f"ready    function {n['function']} ({TIMEOUT_S} s, asynchronous retries 0, MERGE_READS_RAW={env['MERGE_READS_RAW']})")
    sch_role = jobs.ensure_role(iam, n["scheduler_role"], jobs.SCHEDULER_TRUST, {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": "lambda:InvokeFunction", "Resource": fn}]}, SCRIPT, CLEAN)
    sch = sess.client("scheduler")
    jobs.ensure_schedule_group(sch, n["group"], SCRIPT, CLEAN)
    import maintenance
    for d in maintenance.schedule_definitions(prefix(), fn, sch_role):
        for attempt in range(10):
            try:
                state = jobs.ensure_schedule(sch, d)
                break
            except sch.exceptions.ValidationException:
                if attempt == 9:
                    raise
                sleep(6)
        log(f"{state:<8} schedule {n['group']}/{d['Name']}: {d['ScheduleExpression']} {d['ScheduleExpressionTimezone']}")
    sns = sess.client("sns")
    topic = sns.create_topic(Name=n["topic"], Tags=tags("sns"))["TopicArn"]
    record("sns-topic", n["topic"], "created", arn=topic, script=SCRIPT, clean=CLEAN)
    if email:
        sns.subscribe(TopicArn=topic, Protocol="email", Endpoint=email)
        log(f"subscribed {email} to {n['topic']}: confirm the e-mail AWS sends")
    cw = sess.client("cloudwatch")
    for a in alarm_definitions(n["function"], topic):
        cw.put_metric_alarm(**a)
        record("cloudwatch-alarm", a["AlarmName"], "created", script=SCRIPT, clean=CLEAN, bills="monthly")
        log(f"ready    alarm {a['AlarmName']}: {a['MetricName']} {a['ComparisonOperator']} {a['Threshold']:g} "
            f"over {a['Period']} s")
    return {"function_arn": fn, "topic_arn": topic}


def deploy_cli(email: str | None) -> int:
    deploy(email)
    return 0

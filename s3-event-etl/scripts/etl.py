"""Set up, run and hold the S3 event ETL.

    uv run python scripts/etl.py up [--email you@example.com]   # everything, the trigger last
    uv run python scripts/etl.py sweep                          # the hourly sweep, from here
    uv run python scripts/etl.py resume                         # after 3 failed runs
    uv run python scripts/etl.py runs                           # each Glue run: DPU-seconds, cost
    ENGINE=athena ...                                                          # load with Athena, no Glue job

`up` creates (each with the course tags, each recorded in state/ledger.jsonl): the run log (ensure_run_log), the
Glue database <prefix>_lake, the roles, the topic <PREFIX>-etl-alerts, the two functions and their log groups, the
Glue job <PREFIX>-etl (or, with ENGINE=athena, the workgroup and the table trip_events_stage), the schedule group
<PREFIX>-s3-event-etl with the hourly schedule, and last the bucket's notification entry <PREFIX>-s3-event-etl,
added by its Id into the configuration the bucket already has.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parent.parent
for folder in (ROOT / "scripts", ROOT / "pipeline", ROOT / "src"):
    sys.path.insert(0, str(folder))

import control  # noqa: E402
from lake import bucket_name, ensure_workgroup, up as lake_up  # noqa: E402
from run_log import ensure_run_log  # noqa: E402
from tags import prefix, record, recorded, tags  # noqa: E402

REGION = "ca-central-1"
SCRIPT, CLEAN = "scripts/etl.py up", "clean_up.py --yes"
JOB_SCRIPT_KEY = "jobs/s3-event-etl/etl_job.py"
PASSED_ON = ("ETL_MIN_GAP_MIN", "MAX_RUNS_PER_DAY")    # your limits, set in the terminal, reach both functions
GLUE_CONF = ("spark.sql.extensions=org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions"
             " --conf spark.sql.catalog.glue_catalog=org.apache.iceberg.spark.SparkCatalog"
             " --conf spark.sql.catalog.glue_catalog.catalog-impl=org.apache.iceberg.aws.glue.GlueCatalog"
             " --conf spark.sql.catalog.glue_catalog.io-impl=org.apache.iceberg.aws.s3.S3FileIO"
             " --conf spark.sql.catalog.glue_catalog.warehouse=s3://{bucket}/lake/")


def names(p: str | None = None) -> dict:
    p = p or prefix()
    return {"check": f"{p}-etl-check", "sweep": f"{p}-etl-sweep", "job": f"{p}-etl", "topic": f"{p}-etl-alerts",
            "group": f"{p}-s3-event-etl", "schedule": f"{p}-etl-hourly", "entry": f"{p}-s3-event-etl",
            "database": p.replace("-", "_") + "_lake", "roles": {r: f"{p}-etl-{r}-role" for r in
                                                                ("check", "sweep", "glue", "scheduler")}}


def package(deps: bool = True) -> bytes:
    """The functions' zip: etl/*.py, rides/events.py and, for Lambda's arm64 Python 3.12, pydantic."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in ("check.py", "control.py", "sweep.py", "etl_job.py", "athena_load.py"):
            z.write(ROOT / "pipeline" / f, f)
        z.write(ROOT / "src" / "rides" / "__init__.py", "rides/__init__.py")
        z.write(ROOT / "src" / "rides" / "events.py", "rides/events.py")
        if deps:
            with tempfile.TemporaryDirectory() as d:
                subprocess.run(["uv", "pip", "install", "--quiet", "--target", d, "--python-platform",
                                "aarch64-manylinux2014", "--python-version", "3.12", "--only-binary", ":all:",
                                "pydantic>=2.9"], check=True)
                for path in Path(d).rglob("*"):
                    if path.is_file() and "__pycache__" not in path.parts:
                        z.write(path, path.relative_to(d))
    return buf.getvalue()


def _role(iam, name: str, service: str, policy: dict) -> str:
    """The role with one inline policy and nothing attached: a sandbox that lets you create roles may refuse
    iam:AttachRolePolicy (the course's run of 2026-11-23 was refused it), and an inline policy names only our
    resources."""
    trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": service},
                                                      "Action": "sts:AssumeRole"}]}
    try:
        arn = iam.create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps(trust), Tags=tags("iam"))["Role"]["Arn"]
        record("iam-role", name, "created", arn=arn, script=SCRIPT, clean=CLEAN)
    except ClientError as err:
        if err.response["Error"]["Code"] != "EntityAlreadyExists":
            raise
        arn = iam.get_role(RoleName=name)["Role"]["Arn"]
    iam.put_role_policy(RoleName=name, PolicyName="s3-event-etl", PolicyDocument=json.dumps(policy))
    return arn


def glue_job_policy(n: dict, b: str, account: str) -> list[dict]:
    """What the job reads and writes, in place of the managed AWSGlueServiceRole: its prefixes, the Glue catalog
    entries of its own database (the Iceberg catalog reads and updates the table there), and Glue's own log
    groups /aws-glue/*."""
    catalog = f"arn:aws:glue:{REGION}:{account}"
    return [{"Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
             "Resource": [f"{b}/stage/trip-events/*", f"{b}/lake/*", f"{b}/jobs/s3-event-etl/*"]},
            {"Effect": "Allow", "Action": ["s3:ListBucket"], "Resource": b},
            {"Effect": "Allow", "Action": ["glue:GetDatabase", "glue:GetTable", "glue:GetTables", "glue:CreateTable",
                                           "glue:UpdateTable", "glue:GetPartitions"],
             "Resource": [f"{catalog}:catalog", f"{catalog}:database/{n['database']}",
                          f"{catalog}:table/{n['database']}/*"]},
            {"Effect": "Allow", "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
             "Resource": f"arn:aws:logs:{REGION}:{account}:log-group:/aws-glue/*"},
            {"Effect": "Allow", "Action": ["cloudwatch:PutMetricData"], "Resource": "*",
             "Condition": {"StringEquals": {"cloudwatch:namespace": "Glue"}}}]


def roles(sess, n: dict, bucket: str, account: str, sleep=time.sleep) -> dict:
    iam = sess.client("iam")
    b = f"arn:aws:s3:::{bucket}"
    runs = f"arn:aws:dynamodb:{REGION}:{account}:table/{prefix()}-runs"
    logs = f"arn:aws:logs:{REGION}:{account}:log-group:/aws/lambda/{prefix()}-etl-*"
    job = f"arn:aws:glue:{REGION}:{account}:job/{n['job']}"
    topic = f"arn:aws:sns:{REGION}:{account}:{n['topic']}"
    base = [{"Effect": "Allow", "Action": ["s3:GetObject"], "Resource": f"{b}/raw/trip-events/*"},
            {"Effect": "Allow", "Action": ["s3:PutObject", "s3:GetObject"],
             "Resource": [f"{b}/stage/trip-events/*", f"{b}/quarantine/trip-events-rows/*", f"{b}/lake/etl-state/*"]},
            {"Effect": "Allow", "Action": ["s3:ListBucket"], "Resource": b},
            {"Effect": "Allow", "Action": ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem",
                                           "dynamodb:Query"], "Resource": runs},
            {"Effect": "Allow", "Action": ["glue:StartJobRun"], "Resource": job},
            {"Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"], "Resource": f"{logs}:*"}]
    sweep_more = [{"Effect": "Allow", "Action": ["glue:GetJobRun"], "Resource": job},
                  {"Effect": "Allow", "Action": ["sns:Publish"], "Resource": topic},
                  {"Effect": "Allow", "Action": ["athena:StartQueryExecution", "athena:GetQueryExecution"],
                   "Resource": f"arn:aws:athena:{REGION}:{account}:workgroup/{prefix()}-wg"},
                  {"Effect": "Allow", "Action": ["glue:GetDatabase", "glue:GetTable", "glue:GetPartitions",
                                                 "glue:UpdateTable"], "Resource": "*"},
                  # Athena's MERGE runs as this role: it reads the Iceberg table's metadata and writes its files and
                  # the query results, and checks the results bucket first ("Unable to verify/create output bucket"
                  # without GetBucketLocation, live run 2026-11-23)
                  {"Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject",
                                                 "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"],
                   "Resource": [f"{b}/lake/trip_events_clean/*", f"{b}/athena-results/*"]},
                  {"Effect": "Allow", "Action": ["s3:GetBucketLocation"], "Resource": b}]
    doc = lambda st: {"Version": "2012-10-17", "Statement": st}   # noqa: E731
    out = {"check": _role(iam, n["roles"]["check"], "lambda.amazonaws.com", doc(base)),
           "sweep": _role(iam, n["roles"]["sweep"], "lambda.amazonaws.com", doc(base + sweep_more)),
           "glue": _role(iam, n["roles"]["glue"], "glue.amazonaws.com", doc(glue_job_policy(n, b, account))),
           "scheduler": _role(iam, n["roles"]["scheduler"], "scheduler.amazonaws.com", doc([
               {"Effect": "Allow", "Action": ["lambda:InvokeFunction"],
                "Resource": f"arn:aws:lambda:{REGION}:{account}:function:{n['sweep']}"}]))}
    sleep(10)                    # a new role takes a few seconds before a service can assume it (assumed: 10 s)
    return out


def function(sess, name: str, role: str, code: bytes, timeout: int, retries: int, env: dict) -> str:
    lam, logs = sess.client("lambda"), sess.client("logs")
    group = f"/aws/lambda/{name}"
    try:
        logs.create_log_group(logGroupName=group, tags=tags("logs"))
        logs.put_retention_policy(logGroupName=group, retentionInDays=7)
        record("log-group", group, "created", script=SCRIPT, clean=CLEAN)
    except ClientError as err:
        if err.response["Error"]["Code"] != "ResourceAlreadyExistsException":
            raise
    try:
        arn = lam.create_function(FunctionName=name, Runtime="python3.12", Architectures=["arm64"], Role=role,
                                  Handler=f"{name.rsplit('-', 1)[-1]}.handler", Code={"ZipFile": code},
                                  MemorySize=128, Timeout=timeout, Environment={"Variables": env},
                                  Tags=tags("lambda"))["FunctionArn"]
        record("lambda-function", name, "created", arn=arn, script=SCRIPT, clean=CLEAN)
    except ClientError as err:
        if err.response["Error"]["Code"] != "ResourceConflictException":
            raise
        arn = lam.update_function_code(FunctionName=name, ZipFile=code)["FunctionArn"]
        lam.get_waiter("function_updated_v2").wait(FunctionName=name)
        # a second `up` (another ENGINE, other limits) must reach the function's environment too, not only its code
        lam.update_function_configuration(FunctionName=name, Role=role, Timeout=timeout,
                                          Environment={"Variables": env})
        lam.get_waiter("function_updated_v2").wait(FunctionName=name)
    lam.get_waiter("function_active_v2").wait(FunctionName=name)
    lam.put_function_event_invoke_config(FunctionName=name, MaximumRetryAttempts=retries)
    return arn


def wait_ready(sess, name: str, env: dict, tries: int = 8, sleep=time.sleep, log=print) -> None:
    """Invoke the function with {"ready": true} until it answers. A function made a few seconds after its new role
    can fail every call with KMSAccessDeniedException ("unable to decrypt the environment variables": the live run
    of 2026-11-23 lost every S3 event of one landing that way); writing its environment again makes Lambda take a
    new grant, so each failed try does that, then waits longer."""
    lam = sess.client("lambda")
    for attempt in range(1, tries + 1):
        try:
            r = lam.invoke(FunctionName=name, Payload=b'{"ready": true}')
            if not r.get("FunctionError"):
                return
            why = r["Payload"].read().decode()[:200]
        except ClientError as err:
            why = err.response["Error"]["Code"]
        log(f"not yet  {name}: {why}; its environment written again (try {attempt} of {tries})")
        lam.update_function_configuration(FunctionName=name, Environment={"Variables": env})
        lam.get_waiter("function_updated_v2").wait(FunctionName=name)
        sleep(5 * attempt)
    raise SystemExit(f"{name} does not answer after {tries} tries: see its log group /aws/lambda/{name}")


def create_job(sess, n: dict, bucket: str, role: str) -> None:
    """2 G.1X workers, 10 minutes, no retries, one run at a time, queuing off (Glue's timeout would be 480 min)."""
    glue = sess.client("glue")
    spec = dict(Role=role, GlueVersion="5.0", WorkerType="G.1X", NumberOfWorkers=2, Timeout=control.GLUE_TIMEOUT_MIN,
                MaxRetries=0, ExecutionProperty={"MaxConcurrentRuns": 1}, JobRunQueuingEnabled=False,
                Command={"Name": "glueetl", "ScriptLocation": f"s3://{bucket}/{JOB_SCRIPT_KEY}",
                         "PythonVersion": "3"},
                DefaultArguments={"--datalake-formats": "iceberg", "--conf": GLUE_CONF.format(bucket=bucket),
                                  "--enable-metrics": "false", "--job-language": "python"})
    try:
        glue.create_job(Name=n["job"], Tags=tags("glue"), **spec)
        record("glue-job", n["job"], "created", script=SCRIPT, clean=CLEAN)
    except ClientError as err:
        if err.response["Error"]["Code"] != "AlreadyExistsException":
            raise
        glue.update_job(JobName=n["job"], JobUpdate=spec)


def add_entry(s3, bucket: str, entry: dict) -> None:
    """Add or replace our entry by its Id, keep every other one, write the whole configuration back."""
    config = s3.get_bucket_notification_configuration(Bucket=bucket)
    config.pop("ResponseMetadata", None)
    mine = config.get("LambdaFunctionConfigurations", [])
    config["LambdaFunctionConfigurations"] = [e for e in mine if e.get("Id") != entry["Id"]] + [entry]
    try:
        s3.put_bucket_notification_configuration(Bucket=bucket, NotificationConfiguration=config)
    except ClientError as err:
        if "overlap" in str(err).lower():
            others = [e.get("Id") for kind in ("LambdaFunctionConfigurations", "QueueConfigurations",
                                               "TopicConfigurations") for e in config.get(kind, [])
                      if e.get("Id") != entry["Id"]]
            raise SystemExit(f"S3 refused the entry: its prefix and suffix overlap another entry's ({', '.join(others)})")
        raise


def remove_entry(s3, bucket: str, entry_id: str) -> bool:
    config = s3.get_bucket_notification_configuration(Bucket=bucket)
    config.pop("ResponseMetadata", None)
    found = False
    for kind in ("LambdaFunctionConfigurations", "QueueConfigurations", "TopicConfigurations"):
        kept = [e for e in config.get(kind, []) if e.get("Id") != entry_id]
        found |= len(kept) != len(config.get(kind, []))
        config[kind] = kept
    config = {k: v for k, v in config.items() if v}
    s3.put_bucket_notification_configuration(Bucket=bucket, NotificationConfiguration=config)
    return found


def up(sess=None, email: str | None = None, deps: bool = True, log=print, sleep=time.sleep,
       ready: bool | None = None) -> dict:
    """`ready` (default: with `deps`, so in AWS, not offline) invokes each function once before the trigger."""
    sess = sess or boto3.session.Session(region_name=REGION)
    n = names()
    account = sess.client("sts").get_caller_identity()["Account"]
    lake_up(sess, log=log)
    bucket = bucket_name(sess)
    log(f"run log  {ensure_run_log(sess)}")
    glue = sess.client("glue")
    try:
        glue.create_database(DatabaseInput={"Name": n["database"]}, Tags=tags("glue"))
        record("glue-database", n["database"], "created", script=SCRIPT, clean="kept: other recipes' tables")
    except ClientError as err:
        if err.response["Error"]["Code"] != "AlreadyExistsException":
            raise
        if recorded("glue-database", n["database"]) is None:
            record("glue-database", n["database"], "borrowed", script=SCRIPT)
    sns = sess.client("sns")
    topic = sns.create_topic(Name=n["topic"], Tags=tags("sns"))["TopicArn"]
    record("sns-topic", n["topic"], "created", arn=topic, script=SCRIPT, clean=CLEAN)
    if email:
        sns.subscribe(TopicArn=topic, Protocol="email", Endpoint=email)
        log(f"email    {email}: confirm the subscription in the message AWS sends")
    r = roles(sess, n, bucket, account, sleep)
    env = {"PREFIX": prefix(), "ENGINE": control.engine(), "BUCKET": bucket, "ALERTS_TOPIC_ARN": topic,
           **{k: os.environ[k] for k in PASSED_ON if os.environ.get(k)}}
    code = package(deps)
    check_arn = function(sess, n["check"], r["check"], code, 60, 2, env)
    sweep_arn = function(sess, n["sweep"], r["sweep"], code, 330, 0, env)
    if deps if ready is None else ready:
        for fn in (n["check"], n["sweep"]):
            wait_ready(sess, fn, env, sleep=sleep, log=log)
    if control.engine() == "glue":
        sess.client("s3").upload_file(str(ROOT / "pipeline" / "etl_job.py"), bucket, JOB_SCRIPT_KEY)
        create_job(sess, n, bucket, r["glue"])
        log(f"job      {n['job']}: Glue 5.0, 2 G.1X workers, timeout 10 min, no retries, 1 run at a time")
    else:
        log(f"{ensure_workgroup(f'{prefix()}-wg', sess=sess):<8} workgroup {prefix()}-wg")
        athena_stage_table(sess, n, bucket)
        athena_clean_table(sess, n, bucket, log)
    sch = sess.client("scheduler")
    try:
        arn = sch.create_schedule_group(Name=n["group"], Tags=tags("scheduler"))["ScheduleGroupArn"]
        record("schedule-group", n["group"], "created", arn=arn, script=SCRIPT, clean=CLEAN)
    except ClientError as err:
        if err.response["Error"]["Code"] != "ConflictException":
            raise
    try:
        sch.create_schedule(Name=n["schedule"], GroupName=n["group"], ScheduleExpression="rate(1 hour)",
                            FlexibleTimeWindow={"Mode": "OFF"},
                            Target={"Arn": sweep_arn, "RoleArn": r["scheduler"], "RetryPolicy": {
                                "MaximumRetryAttempts": 0}})
    except ClientError as err:
        if err.response["Error"]["Code"] != "ConflictException":
            raise
    lam, s3 = sess.client("lambda"), sess.client("s3")
    try:
        lam.add_permission(FunctionName=n["check"], StatementId="s3-events", Action="lambda:InvokeFunction",
                           Principal="s3.amazonaws.com", SourceArn=f"arn:aws:s3:::{bucket}", SourceAccount=account)
    except ClientError as err:
        if err.response["Error"]["Code"] != "ResourceConflictException":
            raise
    add_entry(s3, bucket, {"Id": n["entry"], "LambdaFunctionArn": check_arn, "Events": ["s3:ObjectCreated:*"],
                           "Filter": {"Key": {"FilterRules": [{"Name": "prefix", "Value": "raw/trip-events/"},
                                                              {"Name": "suffix", "Value": ".jsonl"}]}}})
    record("s3-notification", n["entry"], "created", script=SCRIPT, clean=CLEAN)
    log(f"trigger  {n['entry']}: raw/trip-events/*.jsonl -> {n['check']}")
    return n


def athena_clean_table(sess, n: dict, bucket: str, log=print, sleep=time.sleep) -> None:
    """ENGINE=athena: the Iceberg table trip_events_clean, by one Athena DDL statement in <PREFIX>-wg."""
    import etl_job
    glue = sess.client("glue")
    try:
        glue.get_table(DatabaseName=n["database"], Name=etl_job.TABLE)
        return
    except ClientError as err:
        if err.response["Error"]["Code"] != "EntityNotFoundException":
            raise
    athena = sess.client("athena")
    qid = athena.start_query_execution(QueryString=etl_job.athena_table_ddl(n["database"], bucket),
                                       WorkGroup=f"{prefix()}-wg")["QueryExecutionId"]
    while (q := athena.get_query_execution(QueryExecutionId=qid)["QueryExecution"])["Status"]["State"] in (
            "QUEUED", "RUNNING"):
        sleep(1)
    if q["Status"]["State"] != "SUCCEEDED":
        raise SystemExit(f"Athena could not create {n['database']}.{etl_job.TABLE}: "
                         f"{q['Status'].get('StateChangeReason')}")
    record("glue-table", f"{n['database']}.{etl_job.TABLE}", "created", script=SCRIPT, clean=CLEAN)
    log(f"table    {n['database']}.{etl_job.TABLE}: Iceberg, partitioned by event_day")


def athena_stage_table(sess, n: dict, bucket: str) -> None:
    """ENGINE=athena: trip_events_stage over stage/trip-events/, partition dt by projection."""
    glue = sess.client("glue")
    cols = [("event_id", "string"), ("event_type", "string"), ("trip_id", "string"), ("pickup_zone_id", "int"),
            ("driver_id", "string"), ("dropoff_zone_id", "int"), ("trip_miles", "double"), ("trip_time_s", "int"),
            ("fare", "double"), ("driver_pay", "double"), ("event_time", "string"), ("delivery_id", "string"),
            ("received_time", "string")]
    try:
        glue.create_table(DatabaseName=n["database"], TableInput={
            "Name": "trip_events_stage", "TableType": "EXTERNAL_TABLE",
            "PartitionKeys": [{"Name": "dt", "Type": "string"}],
            "Parameters": {"classification": "json", "projection.enabled": "true", "projection.dt.type": "date",
                           "projection.dt.format": "yyyy-MM-dd", "projection.dt.range": "2024-01-01,NOW",
                           "storage.location.template": f"s3://{bucket}/stage/trip-events/dt=${{dt}}/"},
            "StorageDescriptor": {"Columns": [{"Name": c, "Type": t} for c, t in cols],
                                  "Location": f"s3://{bucket}/stage/trip-events/",
                                  "InputFormat": "org.apache.hadoop.mapred.TextInputFormat",
                                  "OutputFormat": "org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat",
                                  "SerdeInfo": {"SerializationLibrary": "org.openx.data.jsonserde.JsonSerDe"}}})
        record("glue-table", f"{n['database']}.trip_events_stage", "created", script=SCRIPT, clean=CLEAN)
    except ClientError as err:
        if err.response["Error"]["Code"] != "AlreadyExistsException":
            raise


GLUE_USD_PER_DPU_HOUR = 0.44    # Glue 5.0 (below 6.0), ca-central-1: the course price sheet, 2026-11-18
CAD_PER_USD = 1.45              # the course's planning rate
GLUE_WORKER_DPU = {"G.025X": 0.25, "G.1X": 1, "G.2X": 2, "G.4X": 4, "G.8X": 8}


def glue_run_cost(run: dict) -> dict:
    """DPU-seconds and the cost of one JobRun: its DPUSeconds when Glue gives it (the live runs of 2026-11-23 had
    it on a standard 5.0 run), else ExecutionTime (at least the 1-minute minimum) x workers x the worker's DPU."""
    if run.get("DPUSeconds"):
        dpu_s, how = float(run["DPUSeconds"]), "DPUSeconds"
    else:
        dpu = (run.get("NumberOfWorkers") or 2) * GLUE_WORKER_DPU.get(run.get("WorkerType", "G.1X"), 1)
        dpu_s, how = max(run.get("ExecutionTime", 0), 60) * dpu, "ExecutionTime x DPU"
    usd = dpu_s / 3600 * GLUE_USD_PER_DPU_HOUR
    return {"dpu_s": dpu_s, "how": how, "usd": usd, "cad": usd * CAD_PER_USD}


def runs_report(sess, log=print, limit: int = 20) -> float:
    """`etl.py runs`: each run of the job with its ExecutionTime, DPU-seconds and cost; returns the total in USD."""
    glue, total = sess.client("glue"), 0.0
    for r in glue.get_job_runs(JobName=names()["job"], MaxResults=limit).get("JobRuns", []):
        c = glue_run_cost(r)
        total += c["usd"]
        log(f"run      {r['Id'][:12]}  {r['JobRunState']:<9} {r.get('ExecutionTime', 0):>4} s  "
            f"{c['dpu_s']:>6.0f} DPU-s ({c['how']})  {c['usd']:.4f} USD ({c['cad']:.4f} CAD)")
    log(f"total    {total:.4f} USD ({total * CAD_PER_USD:.4f} CAD), {total / 30 * 100:.2f}% of the 30 USD lease")
    return total


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["up", "sweep", "resume", "runs"])
    ap.add_argument("--email", help="up: subscribe this address to the alerts topic")
    args = ap.parse_args(argv)
    sess = boto3.session.Session(region_name=REGION)
    if args.action == "up":
        up(sess, args.email)
    elif args.action == "sweep":
        import sweep
        sweep.sweep(bucket_name(sess), sess)
    elif args.action == "runs":
        runs_report(sess)
    else:
        control.resume(sess)
    return 0


if __name__ == "__main__":
    sys.exit(main())

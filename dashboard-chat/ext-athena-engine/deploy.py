"""Create what ext-athena-engine needs in AWS, as the identity that deploys (never as the reader role).

  uv run python ext-athena-engine/deploy.py --data sim01 --dry-run   # the plan; no AWS call
  uv run python ext-athena-engine/deploy.py --data sim01             # SIM01: one day, three zones
  uv run python ext-athena-engine/deploy.py --data ds01              # DS01: March 2024 pickups, 32 request days

In order: the lake bucket (scripts/lake.py up: created if missing, else borrowed); the workgroup <PREFIX>-chat-wg
(ensure_workgroup: 100 MB cutoff, enforced, engine 3, results under athena-results/chat-wg/); the Parquet files
under chat/ in the bucket; the database <prefix>_chat and tables trips and zones with Athena DDL; the 32 partitions
with MSCK REPAIR TABLE, checked with SHOW PARTITIONS; the table <PREFIX>-chat-usage; the role <PREFIX>-chat-reader
and its policy; the log group (7 days); the function <PREFIX>-chat-query (arm64, Python 3.12, 256 MB, 15 s,
asynchronous retries 0). Every create takes its tags from scripts/tags.py and is written to state/ledger.jsonl.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import time
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Run as a script, Python puts this folder first on sys.path; the parent (for main/) and scripts/ go last, so
# nothing here hides a module of the same name in the parent (its clean_up.py).
sys.path += [p for p in (str(HERE.parent / "scripts"), str(HERE.parent),) if p not in sys.path]

from settings import REGION, Names  # noqa: E402

FUNCTION_MEMORY_MB = 256
FUNCTION_TIMEOUT_S = 15  # above the 10 s a query may wait, below the question's 18 s
LOG_RETENTION_DAYS = 7
PARTITIONS = {"ds01": 32, "sim01": 1}  # DS01: 2024-02-29 to 2024-03-31 by request day


def trust_policy() -> dict:
    return {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"},
                                                    "Action": "sts:AssumeRole"}]}


def reader_policy(names: Names, bucket: str, account: str, region: str = REGION) -> dict:
    """The policy of <PREFIX>-chat-reader (README, "The reader role"): queries in one workgroup, two tables, their
    files, the workgroup's result prefix, the usage table and its own logs. Whether it is the least it needs is
    known only after the live R170 run."""
    db, wg = names.database, names.workgroup
    glue = f"arn:aws:glue:{region}:{account}"
    b = f"arn:aws:s3:::{bucket}"
    return {"Version": "2012-10-17", "Statement": [
        {"Sid": "QueriesInTheChatWorkgroupOnly", "Effect": "Allow",
         "Action": ["athena:StartQueryExecution", "athena:GetQueryExecution", "athena:GetQueryResults",
                    "athena:StopQueryExecution"],
         "Resource": f"arn:aws:athena:{region}:{account}:workgroup/{wg}"},
        {"Sid": "CatalogDatabases", "Effect": "Allow", "Action": ["glue:GetDatabase"],
         "Resource": [f"{glue}:catalog", f"{glue}:database/default", f"{glue}:database/{db}"]},
        {"Sid": "TheTwoTables", "Effect": "Allow", "Action": ["glue:GetTable", "glue:GetPartitions"],
         "Resource": [f"{glue}:catalog", f"{glue}:database/{db}", f"{glue}:table/{db}/trips",
                      f"{glue}:table/{db}/zones"]},
        {"Sid": "ListOwnPrefixes", "Effect": "Allow", "Action": ["s3:ListBucket"], "Resource": b,
         "Condition": {"StringLike": {"s3:prefix": [f"{names.data_prefix}*", f"{names.results_prefix}*"]}}},
        {"Sid": "BucketLocation", "Effect": "Allow", "Action": ["s3:GetBucketLocation"], "Resource": b},
        {"Sid": "ReadTableFiles", "Effect": "Allow", "Action": ["s3:GetObject"],
         "Resource": f"{b}/{names.data_prefix}*"},
        {"Sid": "WriteResults", "Effect": "Allow",
         "Action": ["s3:GetObject", "s3:PutObject", "s3:AbortMultipartUpload"],
         "Resource": f"{b}/{names.results_prefix}*"},
        {"Sid": "DailyCap", "Effect": "Allow", "Action": ["dynamodb:UpdateItem"],
         "Resource": f"arn:aws:dynamodb:{region}:{account}:table/{names.usage_table}"},
        {"Sid": "OwnLogs", "Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
         "Resource": f"arn:aws:logs:{region}:{account}:log-group:{names.log_group}:*"},
    ]}


def ddl(names: Names, bucket: str) -> list[str]:
    """The statements that make the database and the two tables (Athena DDL: the tables carry no tags)."""
    loc = f"s3://{bucket}/{names.data_prefix}"
    return [
        f"CREATE DATABASE IF NOT EXISTS {names.database}",
        f"""CREATE EXTERNAL TABLE IF NOT EXISTS {names.database}.trips (
  trip_id string, trip_state string, pickup_zone_id int, requested_time timestamp, driver_id string,
  fare double, trip_miles double)
PARTITIONED BY (requested_day date)
STORED AS PARQUET
LOCATION '{loc}trips/'""",
        f"""CREATE EXTERNAL TABLE IF NOT EXISTS {names.database}.zones (
  zone_id int, borough string, zone_name string, service_zone string)
STORED AS PARQUET
LOCATION '{loc}zones/'""",
        f"MSCK REPAIR TABLE {names.database}.trips",
    ]


def package() -> bytes:
    """The function's zip: query_fn.py and settings.py (boto3 is in the runtime)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name in ("query_fn.py", "settings.py"):
            z.write(HERE / name, name)
    return buf.getvalue()


def run_statement(athena, sql: str, workgroup: str, database: str | None = None, timeout_s: float = 120) -> dict:
    kw = {"QueryExecutionContext": {"Database": database}} if database else {}
    qid = athena.start_query_execution(QueryString=sql, WorkGroup=workgroup, **kw)["QueryExecutionId"]
    end = time.time() + timeout_s
    while True:
        q = athena.get_query_execution(QueryExecutionId=qid)["QueryExecution"]
        if q["Status"]["State"] in ("SUCCEEDED", "FAILED", "CANCELLED"):
            break
        if time.time() > end:
            athena.stop_query_execution(QueryExecutionId=qid)
            raise SystemExit(f"statement did not finish in {timeout_s} s: {sql[:80]}")
        time.sleep(1)
    if q["Status"]["State"] != "SUCCEEDED":
        raise SystemExit(f"{sql.splitlines()[0][:80]}: {q['Status'].get('StateChangeReason', '')}")
    return q


def plan(names: Names, data: str) -> list[str]:
    return [
        "scripts/lake.py up (bucket <PREFIX>-lake-<account>, created if missing, else borrowed)",
        f"ensure_workgroup({names.workgroup}) (100 MB cutoff, enforced, results athena-results/chat-wg/)",
        f"load.py --data {data}, upload to s3://<bucket>/{names.data_prefix}",
        f"Athena DDL in {names.workgroup}: database {names.database}, tables trips and zones, MSCK REPAIR TABLE",
        f"SHOW PARTITIONS trips lists {PARTITIONS[data]}",
        f"DynamoDB table {names.usage_table} (on demand, key usage_date, TTL expires_at)",
        f"IAM role {names.role} (trusted by lambda.amazonaws.com) with policy reader",
        f"log group {names.log_group} ({LOG_RETENTION_DAYS} days)",
        f"Lambda function {names.function} (arm64, python3.12, {FUNCTION_MEMORY_MB} MB, {FUNCTION_TIMEOUT_S} s, "
        f"asynchronous retries 0)",
    ]


def up(data: str, names: Names | None = None, sess=None, log=print) -> dict:
    import boto3
    import lake
    from tags import record, tags

    names = names or Names()
    if names.database != f"{names.glue_prefix}_chat":
        # README, Decisions (the extension's own table): deploy.py builds and loads <prefix>_chat only. Its DDL, its
        # upload under chat/ and the reader policy's S3 rows do not fit a team's lake table, and the ledger would
        # record that database as created.
        raise SystemExit(f"CHAT_DATABASE is set to {names.database}: deploy.py builds the recipe's own database "
                         f"{names.glue_prefix}_chat only. Unset CHAT_DATABASE to deploy (README, Adapt it).")
    from load import main as load_main

    sess = sess or boto3.Session(region_name=REGION)
    log(f"bucket: {lake.up(sess)}")
    bucket, account = lake.bucket_name(sess), lake.account_id(sess)
    log(f"workgroup {names.workgroup}: {lake.ensure_workgroup(names.workgroup, sess=sess)}")

    loaded = load_main(["--data", data])
    s3 = sess.client("s3")
    for f in sorted(Path(loaded["out"]).rglob("*.parquet")):
        key = names.data_prefix + str(f.relative_to(loaded["out"]))
        s3.upload_file(str(f), bucket, key)
    log(f"uploaded {data} to s3://{bucket}/{names.data_prefix}")
    record("s3-prefix", f"{bucket}/{names.data_prefix}", "created", script="deploy.py",
           clean="ext-athena-engine/clean_up.py")

    athena = sess.client("athena")
    for sql in ddl(names, bucket):
        run_statement(athena, sql, names.workgroup)
    record("glue-database", names.database, "created", script="deploy.py (Athena DDL)",
           clean="ext-athena-engine/clean_up.py, by name")
    q = run_statement(athena, f"SHOW PARTITIONS {names.database}.trips", names.workgroup)
    rows = athena.get_query_results(QueryExecutionId=q["QueryExecutionId"])["ResultSet"]["Rows"]
    if len(rows) != PARTITIONS[data]:
        # Decision (registered partitions): an unregistered day answers COUNT(*) with 0 and no error.
        raise SystemExit(f"SHOW PARTITIONS trips lists {len(rows)}, expected {PARTITIONS[data]}")
    log(f"partitions: {len(rows)}")

    ddb = sess.client("dynamodb")
    try:
        status = ddb.describe_table(TableName=names.usage_table)["Table"]["TableStatus"]
    except ddb.exceptions.ResourceNotFoundException:
        status = None
    if status == "DELETING":
        # clean_up.py ran a moment ago (run.sh does, between SIM01 and DS01): DeleteTable returns before the table is
        # gone, so wait for it and create it again, as main/infra.py's ensure_table does.
        ddb.get_waiter("table_not_exists").wait(TableName=names.usage_table)
        status = None
    if status is not None:
        record("dynamodb-table", names.usage_table, "borrowed", script="deploy.py")
    else:
        ddb.create_table(TableName=names.usage_table,
                         KeySchema=[{"AttributeName": "usage_date", "KeyType": "HASH"}],
                         AttributeDefinitions=[{"AttributeName": "usage_date", "AttributeType": "S"}],
                         BillingMode="PAY_PER_REQUEST", Tags=tags("dynamodb"))
        ddb.get_waiter("table_exists").wait(TableName=names.usage_table)
        ddb.update_time_to_live(TableName=names.usage_table,
                                TimeToLiveSpecification={"Enabled": True, "AttributeName": "expires_at"})
        record("dynamodb-table", names.usage_table, "created", script="deploy.py",
               clean="ext-athena-engine/clean_up.py")

    iam = sess.client("iam")
    try:
        role_arn = iam.get_role(RoleName=names.role)["Role"]["Arn"]
        record("iam-role", names.role, "borrowed", script="deploy.py")
    except iam.exceptions.NoSuchEntityException:
        role_arn = iam.create_role(RoleName=names.role, AssumeRolePolicyDocument=json.dumps(trust_policy()),
                                   Tags=tags("iam"), Description="ext-athena-engine: the chat's query reader")["Role"]["Arn"]
        record("iam-role", names.role, "created", arn=role_arn, script="deploy.py", clean="ext-athena-engine/clean_up.py")
    iam.put_role_policy(RoleName=names.role, PolicyName="reader",
                        PolicyDocument=json.dumps(reader_policy(names, bucket, account)))

    logs = sess.client("logs")
    try:
        logs.create_log_group(logGroupName=names.log_group, tags=tags("logs"))
        record("log-group", names.log_group, "created", script="deploy.py", clean="ext-athena-engine/clean_up.py")
    except logs.exceptions.ResourceAlreadyExistsException:
        record("log-group", names.log_group, "borrowed", script="deploy.py")
    logs.put_retention_policy(logGroupName=names.log_group, retentionInDays=LOG_RETENTION_DAYS)

    lam = sess.client("lambda")
    env = {"Variables": {"WORKGROUP": names.workgroup, "DATABASE": names.database,
                         "USAGE_TABLE": names.usage_table}}
    try:
        lam.get_function(FunctionName=names.function)
        lam.update_function_code(FunctionName=names.function, ZipFile=package())
        lam.get_waiter("function_updated_v2").wait(FunctionName=names.function)
        lam.update_function_configuration(FunctionName=names.function, Environment=env)
        record("lambda-function", names.function, "borrowed", script="deploy.py (code updated)")
    except lam.exceptions.ResourceNotFoundException:
        for attempt in range(10):  # a new role takes a few seconds before Lambda may assume it
            try:
                arn = lam.create_function(
                    FunctionName=names.function, Runtime="python3.12", Architectures=["arm64"], Role=role_arn,
                    Handler="query_fn.handler", Code={"ZipFile": package()}, MemorySize=FUNCTION_MEMORY_MB,
                    Timeout=FUNCTION_TIMEOUT_S, Environment=env, Tags=tags("lambda"))["FunctionArn"]
                break
            except lam.exceptions.InvalidParameterValueException:
                if attempt == 9:
                    raise
                time.sleep(3)
        record("lambda-function", names.function, "created", arn=arn, script="deploy.py",
               clean="ext-athena-engine/clean_up.py")
        lam.get_waiter("function_active_v2").wait(FunctionName=names.function)
    lam.put_function_event_invoke_config(FunctionName=names.function, MaximumRetryAttempts=0)
    log(f"function {names.function} ready")
    return {"bucket": bucket, "partitions": len(rows)}


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data", choices=["sim01", "ds01"], default="sim01")
    p.add_argument("--dry-run", action="store_true", help="print the plan; no AWS call")
    args = p.parse_args(argv)
    names = Names()
    if args.dry_run:
        for i, step in enumerate(plan(names, args.data), 1):
            print(f"{i}. {step}")
        return
    up(args.data, names)


if __name__ == "__main__":
    main()

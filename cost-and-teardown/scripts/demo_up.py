"""Create the demonstration system: every create carries the course tags and writes a ledger row.

    PREFIX=cost-yourname uv run python scripts/demo_up.py     # TEAM defaults to PREFIX, OWNER to your login

Kinesis stream <PREFIX>-cost-events (provisioned, 1 shard) -> function <PREFIX>-cost-count (arm64, 128 MB, 60 s)
-> table <PREFIX>-cost-counts; the lake bucket (scripts/lake.py up: created, or borrowed if it is there), the
workgroup <PREFIX>-wg (scripts/lake.py ensure_workgroup), and the Glue database <prefix>_cost with a table over
raw/trip-events/, which take no tags and are found by name. A resource that is already there is recorded
`borrowed` and left as it is.
"""
from __future__ import annotations

import io
import json
import sys
import time
import zipfile
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lake import bucket_name, ensure_workgroup, up  # noqa: E402
from tags import prefix, record, recorded, tags  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
REGION = "ca-central-1"
SCRIPT = "scripts/demo_up.py"
CLEAN = "clean_up.py --yes"


def names(p: str | None = None) -> dict:
    p = p or prefix()
    return {"stream": f"{p}-cost-events", "function": f"{p}-cost-count", "table": f"{p}-cost-counts",
            "role": f"{p}-cost-role", "log_group": f"/aws/lambda/{p}-cost-count",
            "database": p.replace("-", "_") + "_cost", "glue_table": "trip_events_demo", "workgroup": f"{p}-wg"}


def package() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.write(ROOT / "function" / "handler.py", "handler.py")
    return buf.getvalue()


def _exists(err: ClientError, *codes) -> bool:
    return err.response["Error"]["Code"] in codes


def up_all(sess=None, log=print, sleep=time.sleep) -> dict:
    sess = sess or boto3.session.Session(region_name=REGION)
    n = names()
    account = sess.client("sts").get_caller_identity()["Account"]
    bucket = bucket_name(sess)
    up(sess, log=log)
    log(f"{ensure_workgroup(n['workgroup'], sess=sess):<8} workgroup {n['workgroup']}")

    kinesis = sess.client("kinesis")
    try:
        kinesis.create_stream(StreamName=n["stream"], ShardCount=1, StreamModeDetails={"StreamMode": "PROVISIONED"},
                              Tags=tags("kinesis"))
        kinesis.get_waiter("stream_exists").wait(StreamName=n["stream"])
        state = "created"
    except ClientError as err:
        if not _exists(err, "ResourceInUseException"):
            raise
        state = "borrowed"
    stream_arn = kinesis.describe_stream_summary(StreamName=n["stream"])["StreamDescriptionSummary"]["StreamARN"]
    record("kinesis-stream", n["stream"], state, arn=stream_arn, script=SCRIPT, clean=CLEAN, bills="hourly")
    log(f"{state:<8} stream {n['stream']} (1 shard, 0.0165 USD an hour while it exists)")

    ddb = sess.client("dynamodb")
    try:
        table_arn = ddb.create_table(TableName=n["table"], BillingMode="PAY_PER_REQUEST",
                                     AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
                                     KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
                                     Tags=tags("dynamodb"))["TableDescription"]["TableArn"]
        ddb.get_waiter("table_exists").wait(TableName=n["table"])
        state = "created"
    except ClientError as err:
        if not _exists(err, "ResourceInUseException"):
            raise
        table_arn, state = ddb.describe_table(TableName=n["table"])["Table"]["TableArn"], "borrowed"
    record("dynamodb-table", n["table"], state, arn=table_arn, script=SCRIPT, clean=CLEAN, bills="monthly")
    log(f"{state:<8} table {n['table']} (on demand)")

    iam = sess.client("iam")
    trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"},
                                                      "Action": "sts:AssumeRole"}]}
    policy = {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": ["kinesis:GetRecords", "kinesis:GetShardIterator", "kinesis:DescribeStream",
                                       "kinesis:DescribeStreamSummary", "kinesis:ListShards"], "Resource": stream_arn},
        {"Effect": "Allow", "Action": ["dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:ConditionCheckItem"],
         "Resource": table_arn},
        {"Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
         "Resource": f"arn:aws:logs:{REGION}:{account}:log-group:{n['log_group']}:*"}]}
    try:
        role_arn = iam.create_role(RoleName=n["role"], AssumeRolePolicyDocument=json.dumps(trust),
                                   Tags=tags("iam"))["Role"]["Arn"]
        state = "created"
    except ClientError as err:
        if not _exists(err, "EntityAlreadyExists"):
            raise
        role_arn, state = iam.get_role(RoleName=n["role"])["Role"]["Arn"], "borrowed"
    if state == "created":
        iam.put_role_policy(RoleName=n["role"], PolicyName="cost-count", PolicyDocument=json.dumps(policy))
        sleep(10)               # a new role takes a few seconds before Lambda can assume it (assumed: 10 s)
    record("iam-role", n["role"], state, arn=role_arn, script=SCRIPT, clean=CLEAN)
    log(f"{state:<8} role {n['role']}")

    logs = sess.client("logs")
    try:
        logs.create_log_group(logGroupName=n["log_group"], tags=tags("logs"))
        logs.put_retention_policy(logGroupName=n["log_group"], retentionInDays=7)
        state = "created"
    except ClientError as err:
        if not _exists(err, "ResourceAlreadyExistsException"):
            raise
        state = "borrowed"
    record("log-group", n["log_group"], state, arn=f"arn:aws:logs:{REGION}:{account}:log-group:{n['log_group']}",
           script=SCRIPT, clean=CLEAN)
    log(f"{state:<8} log group {n['log_group']} (7 days)")

    lam = sess.client("lambda")
    try:
        fn_arn = lam.create_function(FunctionName=n["function"], Runtime="python3.12", Architectures=["arm64"],
                                     Role=role_arn, Handler="handler.handler", Code={"ZipFile": package()},
                                     MemorySize=128, Timeout=60, Environment={"Variables": {"TABLE": n["table"]}},
                                     Tags=tags("lambda"))["FunctionArn"]
        lam.get_waiter("function_active_v2").wait(FunctionName=n["function"])
        state = "created"
    except ClientError as err:
        if not _exists(err, "ResourceConflictException"):
            raise
        fn_arn, state = lam.get_function(FunctionName=n["function"])["Configuration"]["FunctionArn"], "borrowed"
    record("lambda-function", n["function"], state, arn=fn_arn, script=SCRIPT, clean=CLEAN)
    log(f"{state:<8} function {n['function']} (arm64, 128 MB, timeout 60 s)")

    mappings = lam.list_event_source_mappings(FunctionName=n["function"], EventSourceArn=stream_arn)
    if mappings["EventSourceMappings"]:
        m, state = mappings["EventSourceMappings"][0], "borrowed"
    else:
        m = lam.create_event_source_mapping(FunctionName=n["function"], EventSourceArn=stream_arn,
                                            StartingPosition="LATEST", BatchSize=100, Tags=tags("lambda"))
        state = "created"
    esm_arn = m.get("EventSourceMappingArn") or f"arn:aws:lambda:{REGION}:{account}:event-source-mapping:{m['UUID']}"
    record("lambda-esm", f"{n['stream']} to {n['function']}", state, arn=esm_arn, script=SCRIPT, clean=CLEAN)
    log(f"{state:<8} event source mapping {n['stream']} to {n['function']}")

    glue = sess.client("glue")
    try:
        glue.create_database(DatabaseInput={"Name": n["database"]})
        state = "created"
    except ClientError as err:
        if not _exists(err, "AlreadyExistsException"):
            raise
        state = "borrowed"
    record("glue-database", n["database"], state, script=SCRIPT, clean=CLEAN)
    columns = [{"Name": c, "Type": t} for c, t in (("event_type", "string"), ("event_id", "string"),
                                                   ("trip_id", "string"), ("event_time", "string"),
                                                   ("pickup_zone_id", "int"), ("delivery_id", "string"))]
    try:
        glue.create_table(DatabaseName=n["database"], TableInput={
            "Name": n["glue_table"], "TableType": "EXTERNAL_TABLE",
            "Parameters": {"classification": "json"},
            "StorageDescriptor": {"Columns": columns, "Location": f"s3://{bucket}/raw/trip-events/",
                                  "InputFormat": "org.apache.hadoop.mapred.TextInputFormat",
                                  "OutputFormat": "org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat",
                                  "SerdeInfo": {"SerializationLibrary": "org.openx.data.jsonserde.JsonSerDe"}}})
        state = "created"
    except ClientError as err:
        if not _exists(err, "AlreadyExistsException"):
            raise
        state = "borrowed"
    record("glue-table", f"{n['database']}.{n['glue_table']}", state, script=SCRIPT, clean=CLEAN)
    log(f"{state:<8} Glue database {n['database']} and table {n['glue_table']} (no tags: found by name)")
    return n


def main(argv=None) -> int:
    up_all()
    print("next: uv run python sim/replay.py SIM01 --to kinesis --target <stream> ...; see README, Run it live")
    return 0


if __name__ == "__main__":
    sys.exit(main())

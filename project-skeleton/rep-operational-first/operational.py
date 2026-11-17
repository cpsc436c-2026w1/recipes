"""Operational first: updates -> API (Lambda function URL) -> DynamoDB current state -> export to S3 -> Athena.

The API answers the number from the table; the lake answers the same question from the export. Read with the README
of the recipe, section Decisions; each "Decision:" comment below names its row.
"""
import io
import json
import os
import time
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.exceptions import ClientError

import handler
from skeleton.aws import athena_query, ensure_bucket, ensure_role, ensure_workgroup, wait_until_gone
from skeleton.config import REGION, TAG
from skeleton.lake_local import query, write_export
from skeleton.number import export_sql

HERE = Path(__file__).resolve().parent

# The export table names only the attributes the queries use; DynamoDB JSON types each value ({"S": ...}).
EXPORT_COLUMNS = ("pk:struct<S:string>, kind:struct<S:string>, event_id:struct<S:string>, "
                  "entity_id:struct<S:string>, event_time:struct<S:string>, n:struct<N:string>, "
                  "last_event_time:struct<S:string>")


def create_table(ddb, name: str, sleep=time.sleep) -> None:
    """Decision "capacity mode": on-demand, billed per request, nothing to size before the load is known.

    A table that is there is used; one that a clean-up a moment ago is still deleting is waited out first.
    """
    for _ in range(2):
        try:
            ddb.create_table(TableName=name, BillingMode="PAY_PER_REQUEST",
                             KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
                             AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}], Tags=[TAG])
            break
        except ddb.exceptions.ResourceInUseException:
            try:
                status = ddb.describe_table(TableName=name)["Table"]["TableStatus"]
            except ddb.exceptions.ResourceNotFoundException:
                continue                    # the deletion finished between the two calls: create it now
            if status != "DELETING":
                break
            wait_until_gone(lambda: ddb.describe_table(TableName=name), ddb.exceptions.ResourceNotFoundException,
                            sleep)
    ddb.get_waiter("table_exists").wait(TableName=name)


# ---------------------------------------------------------------- AWS ------------------------------------------------

def api_policy(n: dict, account: str) -> dict:
    """Decision "permissions": the function may touch this one table and write its own log group, nothing else."""
    return {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Resource": f"arn:aws:dynamodb:{REGION}:{account}:table/{n['table']}",
         "Action": ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:BatchGetItem",
                    "dynamodb:ConditionCheckItem"]},
        {"Effect": "Allow", "Resource": f"arn:aws:logs:{REGION}:{account}:log-group:{n['log_group']}:*",
         "Action": ["logs:CreateLogStream", "logs:PutLogEvents"]},
    ]}


def setup(sess, n: dict, account: str, sleep=time.sleep) -> str:
    """Table, export settings, function and URL. Returns the function URL."""
    ddb = sess.client("dynamodb")
    create_table(ddb, n["table"], sleep)
    # Decision "lake copy": exports need point-in-time recovery; TTL removes old event items by itself.
    ddb.update_continuous_backups(TableName=n["table"],
                                  PointInTimeRecoverySpecification={"PointInTimeRecoveryEnabled": True})
    try:
        ddb.update_time_to_live(TableName=n["table"],
                                TimeToLiveSpecification={"Enabled": True, "AttributeName": "expires_at"})
    except ClientError as e:
        if "already enabled" not in str(e):
            raise
    ensure_bucket(sess.client("s3"), n["bucket"])
    logs = sess.client("logs")
    try:
        logs.create_log_group(logGroupName=n["log_group"], tags={TAG["Key"]: TAG["Value"]})
    except logs.exceptions.ResourceAlreadyExistsException:
        pass
    logs.put_retention_policy(logGroupName=n["log_group"], retentionInDays=7)   # ours: a week of logs is enough

    role = ensure_role(sess.client("iam"), n["function_role"], "lambda.amazonaws.com", api_policy(n, account),
                       "API_ROLE_ARN")
    lam = sess.client("lambda")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(HERE / "handler.py", "handler.py")
    for attempt in range(6):
        try:
            lam.create_function(FunctionName=n["function"], Runtime="python3.12", Handler="handler.handler",
                                Role=role, Code={"ZipFile": buf.getvalue()}, Architectures=["arm64"],
                                Timeout=10, MemorySize=256, Environment={"Variables": {"TABLE": n["table"]}},
                                Tags={TAG["Key"]: TAG["Value"]})
            break
        except lam.exceptions.ResourceConflictException:          # exists: deploy the current code
            lam.update_function_code(FunctionName=n["function"], ZipFile=buf.getvalue())
            break
        except lam.exceptions.InvalidParameterValueException:     # a new role may not be assumable yet
            if attempt == 5:
                raise
            sleep(10)
    lam.get_waiter("function_active_v2").wait(FunctionName=n["function"])
    # Decision "who may call": AWS_IAM, so only signed requests from your account reach the function.
    try:
        return lam.create_function_url_config(FunctionName=n["function"], AuthType="AWS_IAM")["FunctionUrl"]
    except lam.exceptions.ResourceConflictException:
        return lam.get_function_url_config(FunctionName=n["function"])["FunctionUrl"]


def call(sess, url: str, method: str, path: str, body: dict | None = None, params: dict | None = None) -> dict:
    """One signed request to the function URL (Signature Version 4, service "lambda")."""
    full = url.rstrip("/") + path + (("?" + urlencode(params)) if params else "")
    data = json.dumps(body).encode() if body is not None else None
    req = AWSRequest(method=method, url=full, data=data, headers={"content-type": "application/json"})
    SigV4Auth(sess.get_credentials().get_frozen_credentials(), "lambda", REGION).add_auth(req)
    r = urllib.request.Request(full, data=data, method=method, headers=dict(req.headers))
    with urllib.request.urlopen(r, timeout=30) as resp:
        return json.loads(resp.read())


def send(sess, url: str, events: list[dict]) -> dict:
    total = {"stored": 0, "duplicates": 0, "older": 0}
    for i in range(0, len(events), handler.MAX_BATCH):
        r = call(sess, url, "POST", "/updates", {"updates": events[i:i + handler.MAX_BATCH]})
        for k in total:
            total[k] += r[k]
    return total


def export(sess, n: dict, account: str, timeout_s: int = 1800, sleep=time.sleep) -> str:
    """A full export of the table to s3://<bucket>/exports/. Returns the S3 folder of its data files.

    Decision "lake copy": an export does not use the table's read capacity; it is as fresh as the moment it ran.
    """
    ddb = sess.client("dynamodb")
    arn = f"arn:aws:dynamodb:{REGION}:{account}:table/{n['table']}"
    for attempt in range(10):            # PITR takes a moment after it is turned on
        try:
            desc = ddb.export_table_to_point_in_time(TableArn=arn, S3Bucket=n["bucket"], S3Prefix="exports",
                                                     ExportFormat="DYNAMODB_JSON", ExportType="FULL_EXPORT")
            break
        except ddb.exceptions.PointInTimeRecoveryUnavailableException:
            sleep(30)
    else:
        raise RuntimeError("point-in-time recovery is still unavailable; try again in a few minutes")
    export_arn = desc["ExportDescription"]["ExportArn"]
    deadline = time.time() + timeout_s
    while True:
        d = ddb.describe_export(ExportArn=export_arn)["ExportDescription"]
        if d["ExportStatus"] == "COMPLETED":
            break
        if d["ExportStatus"] == "FAILED":
            raise RuntimeError(f"export failed: {d.get('FailureMessage')}")
        if time.time() > deadline:
            raise TimeoutError(f"export still {d['ExportStatus']} after {timeout_s} s")
        sleep(20)
    return f"s3://{n['bucket']}/exports/AWSDynamoDB/{export_arn.rsplit('/', 1)[-1]}/data/"


def point_lake_at(sess, n: dict, data_folder: str) -> None:
    """The Athena table `updates_export` reads the newest export only (older exports hold older copies)."""
    athena = sess.client("athena")
    glue = sess.client("glue")
    try:
        glue.create_database(DatabaseInput={"Name": n["database"]}, Tags={TAG["Key"]: TAG["Value"]})
    except glue.exceptions.AlreadyExistsException:
        pass
    ensure_workgroup(athena, n["workgroup"], n["bucket"])
    athena_query(athena, "DROP TABLE IF EXISTS updates_export", n["workgroup"], n["database"])
    athena_query(athena, f"CREATE EXTERNAL TABLE updates_export (Item struct<{EXPORT_COLUMNS}>) "
                         "ROW FORMAT SERDE 'org.openx.data.jsonserde.JsonSerDe' "
                         f"LOCATION '{data_folder}'", n["workgroup"], n["database"])


def number_lake_aws(sess, n: dict, start: datetime, end: datetime) -> tuple[int, int]:
    rows, scanned = athena_query(sess.client("athena"), export_sql(start, end), n["workgroup"], n["database"])
    return int(rows[0]["updates_last_hour"]), scanned


# ---------------------------------------------------------------- offline --------------------------------------------

def url_event(method: str, path: str, body: dict | None = None, params: dict | None = None) -> dict:
    """What a function URL hands the handler (payload format 2.0), built by hand."""
    return {"requestContext": {"http": {"method": method}}, "rawPath": path, "queryStringParameters": params,
            "body": json.dumps(body) if body is not None else None, "isBase64Encoded": False}


def invoke(method: str, path: str, body: dict | None = None, params: dict | None = None) -> dict:
    r = handler.route(url_event(method, path, body, params))     # the answer without the log line Lambda keeps
    return {"status": r["statusCode"], **json.loads(r["body"])}


def run_local(events: list[dict], root: Path, as_of: datetime, table: str = "recipe-updates") -> dict:
    """The same path without AWS: moto plays DynamoDB in this process, the handler runs as a plain function,
    a scan written in the export's format plays the export, and DuckDB plays Athena.

    Call it inside `moto.mock_aws()` (run.py does).
    """
    import shutil

    import boto3
    shutil.rmtree(root / "exports", ignore_errors=True)       # each run starts from an empty local lake
    os.environ["TABLE"] = table
    ddb = boto3.client("dynamodb", region_name=REGION)
    handler._client = ddb
    create_table(ddb, table)
    sent = {"stored": 0, "duplicates": 0, "older": 0}
    for i in range(0, len(events), handler.MAX_BATCH):
        r = invoke("POST", "/updates", {"updates": events[i:i + handler.MAX_BATCH]})
        for k in sent:
            sent[k] += r[k]
    api = invoke("GET", "/number", params={"as_of": as_of.strftime("%Y-%m-%dT%H:%M:%S.000Z")})
    items, kw = [], {}
    while True:                       # a Scan here only builds the stand-in export; the real one does not scan
        page = ddb.scan(TableName=table, **kw)
        items += page["Items"]
        if "LastEvaluatedKey" not in page:
            break
        kw = {"ExclusiveStartKey": page["LastEvaluatedKey"]}
    folder = write_export(items, root, export_id=f"local-{int(time.time())}")
    start, end = handler.window(as_of)
    lake = int(query(root, export_sql(start, end))[0]["updates_last_hour"])
    return {"sent": sent, "api": api, "lake": lake, "export_folder": str(folder), "items": len(items)}

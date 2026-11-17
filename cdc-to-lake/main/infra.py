"""Create the real pipeline in your sandbox: table with a stream, bucket, Iceberg table, Firehose stream, Lambda.

Written from the AWS API references (DynamoDB, Firehose IcebergDestinationConfiguration, Lambda
CreateEventSourceMapping, Athena Iceberg DDL) and the boto3 service models. output/main.txt is a run of it on AWS.
Every name comes from PREFIX (main/config.py); clean_up.py removes all of it. Each step can run again: an existing
resource is kept.

Roles: ensure_roles() creates one role per service, each allowed only what that service does here (SEC03-BP02),
tagged recipe=cdc-to-lake so clean_up.py removes them. To use roles you already have instead, set
  LAMBDA_ROLE_ARN    reads the stream (dynamodb:GetRecords, GetShardIterator, DescribeStream, ListStreams),
                     writes Firehose (firehose:PutRecordBatch), sends to the failures queue (sqs:SendMessage),
                     writes its logs
  FIREHOSE_ROLE_ARN  Glue (GetTable, GetDatabase, UpdateTable) and S3 (Get/Put/List/DeleteObject) on the bucket
  SCHEDULER_ROLE_ARN (ext-maintenance --schedule) athena:StartQueryExecution, Glue and S3 on the lake
and no role is created for that service.
"""

from __future__ import annotations

import dataclasses
import io
import json
import time
import zipfile

import boto3

from main import athena as athena_q
from main.config import ROOT, Settings, sql


MAX_UNITS = 100     # ours: the most read and write request units per second the trips table takes


def account_id(region: str) -> str:
    return boto3.client("sts", region_name=region).get_caller_identity()["Account"]


def create_table(ddb, s: Settings, poll_s: float = 5.0) -> str:
    """The operational table, with its change stream on. Returns the stream ARN."""
    for _ in range(2):
        try:
            ddb.create_table(
                TableName=s.table,
                KeySchema=[{"AttributeName": "trip_id", "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": "trip_id", "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
                # A ceiling on the table's throughput: a loop that writes without end is throttled at this rate
                # instead of billing without limit (README, Decisions: "The table's capacity").
                OnDemandThroughput={"MaxReadRequestUnits": MAX_UNITS, "MaxWriteRequestUnits": MAX_UNITS},
                # NEW_AND_OLD_IMAGES: a REMOVE record still carries the removed item's values (main/handler.py).
                StreamSpecification={"StreamEnabled": True, "StreamViewType": "NEW_AND_OLD_IMAGES"},
                Tags=[{"Key": "recipe", "Value": "cdc-to-lake"}],
            )
            break
        except ddb.exceptions.ResourceInUseException:
            # ResourceInUseException is the answer for a table that is there (API reference, CreateTable). We also
            # take it for one that a clean-up a moment ago is still deleting, which that page does not say: that
            # one is waited out (5 minutes at most) and created again, once.
            try:
                status = ddb.describe_table(TableName=s.table)["Table"]["TableStatus"]
            except ddb.exceptions.ResourceNotFoundException:
                continue
            if status != "DELETING":
                break
            ddb.get_waiter("table_not_exists").wait(
                TableName=s.table, WaiterConfig={"Delay": poll_s, "MaxAttempts": int(300 / max(poll_s, 1))})
    ddb.get_waiter("table_exists").wait(
        TableName=s.table, WaiterConfig={"Delay": poll_s, "MaxAttempts": int(300 / max(poll_s, 1))})
    return ddb.describe_table(TableName=s.table)["Table"]["LatestStreamArn"]


RESULT_DAYS = 7     # ours: how long Athena's result files are kept in a bucket the recipe created


def create_bucket(s3, bucket: str, region: str, own: bool = True) -> None:
    """The lake's bucket. own=False: the bucket was passed in as BUCKET, so its rules are the team's and stay."""
    try:
        s3.create_bucket(Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": region})
    except (s3.exceptions.BucketAlreadyOwnedByYou,):
        pass
    if own:
        # Every Athena query writes a result file and a metadata file under athena-results/, and nothing reads
        # them after the run. S3 deletes them by itself after RESULT_DAYS days.
        s3.put_bucket_lifecycle_configuration(Bucket=bucket, LifecycleConfiguration={"Rules": [{
            "ID": "expire-athena-results", "Status": "Enabled", "Filter": {"Prefix": "athena-results/"},
            "Expiration": {"Days": RESULT_DAYS}}]})
    s3.put_public_access_block(Bucket=bucket, PublicAccessBlockConfiguration={
        "BlockPublicAcls": True, "IgnorePublicAcls": True, "BlockPublicPolicy": True, "RestrictPublicBuckets": True})


def create_lake_table(glue, athena, s: Settings, account: str) -> None:
    try:
        glue.create_database(DatabaseInput={"Name": s.database, "Description": "cdc-to-lake recipe"})
    except glue.exceptions.AlreadyExistsException:
        pass
    out = s.athena_output(account)
    athena_q.run(athena, sql("create_table", database=s.database, table=s.lake_table,
                             location=s.location(account)), s.workgroup, out)
    athena_q.run(athena, sql("current_view", view=f"{s.database}.{s.view}", table=f"{s.database}.{s.lake_table}"),
                 s.workgroup, out)


def firehose_config(s: Settings, account: str) -> dict:
    """IcebergDestinationConfiguration for the change log (Firehose API reference)."""
    bucket = s.bucket(account)
    return {
        "RoleARN": s.firehose_role_arn,
        "CatalogConfiguration": {"CatalogARN": f"arn:aws:glue:{s.region}:{account}:catalog"},
        "DestinationTableConfigurationList": [{
            "DestinationDatabaseName": s.database,
            "DestinationTableName": s.lake_table,
            "S3ErrorOutputPrefix": "firehose-errors/",
        }],
        # Every change is a new row, never an update in place, so the stream is append-only. AWS: with
        # AppendOnly, Firehose raises the stream's throughput limit on its own; it must not be set when records
        # update or delete rows (README, "Decisions").
        "AppendOnly": True,
        "BufferingHints": {"IntervalInSeconds": s.buffer_seconds, "SizeInMBs": 1},
        "RetryOptions": {"DurationInSeconds": 300},
        "S3BackupMode": "FailedDataOnly",  # records Firehose could not write land under firehose-errors/
        "S3Configuration": {"RoleARN": s.firehose_role_arn, "BucketARN": f"arn:aws:s3:::{bucket}",
                            "ErrorOutputPrefix": "firehose-errors/"},
    }


def firehose_status(firehose, name: str) -> str | None:
    """The stream's status, or None when there is no such stream."""
    try:
        return firehose.describe_delivery_stream(DeliveryStreamName=name)[
            "DeliveryStreamDescription"]["DeliveryStreamStatus"]
    except firehose.exceptions.ResourceNotFoundException:
        return None


def create_firehose(firehose, s: Settings, account: str, wait_s: int = 300, poll_s: float = 10.0) -> None:
    """Create the stream and wait until it is active. A stream with this name that a clean-up a moment ago is still
    deleting is waited out first, then created again."""
    deadline = time.time() + wait_s
    while time.time() < deadline:
        status = firehose_status(firehose, s.delivery_stream)
        if status == "ACTIVE":
            return
        if status is None:
            try:
                firehose.create_delivery_stream(
                    DeliveryStreamName=s.delivery_stream, DeliveryStreamType="DirectPut",
                    IcebergDestinationConfiguration=firehose_config(s, account),
                    Tags=[{"Key": "recipe", "Value": "cdc-to-lake"}])
            except firehose.exceptions.ResourceInUseException:
                pass                                  # made by another run between the two calls
            continue
        if status.endswith("FAILED"):
            raise RuntimeError(f"Firehose stream {s.delivery_stream}: {status}")
        time.sleep(poll_s)                            # CREATING, or DELETING: look again
    raise TimeoutError(f"Firehose stream {s.delivery_stream} not active after {wait_s} s")


def create_failures_queue(sqs, s: Settings, wait_s: float = 90.0, poll_s: float = 5.0) -> str:
    """The queue Lambda writes to when it gives up on a batch. Returns its ARN. A queue that is there is used.

    SQS refuses a name for 60 seconds after a queue with it was deleted (a run right after a clean-up): that
    refusal is waited out.
    """
    end = time.time() + wait_s
    while True:
        try:
            url = sqs.create_queue(QueueName=s.failures_queue, tags={"recipe": "cdc-to-lake"})["QueueUrl"]
            break
        except sqs.exceptions.QueueDeletedRecently:
            if time.time() >= end:
                raise
            time.sleep(poll_s)
    return sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]


def function_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(ROOT / "main" / "handler.py", "handler.py")
    return buf.getvalue()


def create_function(lam, s: Settings, stream_arn: str, failures_arn: str | None = None) -> None:
    try:
        lam.create_function(
            FunctionName=s.function, Runtime="python3.12", Role=s.lambda_role_arn, Handler="handler.handler",
            Code={"ZipFile": function_zip()}, Timeout=60, MemorySize=256,
            Environment={"Variables": {"DELIVERY_STREAM": s.delivery_stream}},
            Tags={"recipe": "cdc-to-lake"})
        lam.get_waiter("function_active_v2").wait(FunctionName=s.function)
    except lam.exceptions.ResourceConflictException:
        pass
    existing = lam.list_event_source_mappings(FunctionName=s.function, EventSourceArn=stream_arn)["EventSourceMappings"]
    if existing:
        # A mapping made by an earlier run (perhaps from before the failures queue) gets the queue as well.
        for mapping in existing:
            if failures_arn and mapping.get("DestinationConfig", {}).get("OnFailure", {}).get("Destination") != failures_arn:
                lam.update_event_source_mapping(UUID=mapping["UUID"],
                                                DestinationConfig={"OnFailure": {"Destination": failures_arn}})
        return
    lam.create_event_source_mapping(
        FunctionName=s.function, EventSourceArn=stream_arn,
        # TRIM_HORIZON: start from the oldest record the stream still holds (24 hours), so changes made before
        # the mapping existed still reach the lake. LATEST would skip them.
        StartingPosition="TRIM_HORIZON",
        BatchSize=100,
        MaximumBatchingWindowInSeconds=5,
        FunctionResponseTypes=["ReportBatchItemFailures"],  # main/handler.py returns the first failed record
        BisectBatchOnFunctionError=True,
        # Give up on a batch after 10 retries (11 tries in all: MaximumRetryAttempts counts the retries), so one
        # bad record cannot hold back the shard until it leaves the stream after 24 hours. What Lambda gives up on is
        # not lost without a trace: it writes the batch's position in the stream (shard and sequence numbers, not the
        # records) to the failures queue.
        MaximumRetryAttempts=10,
        **({"DestinationConfig": {"OnFailure": {"Destination": failures_arn}}} if failures_arn else {}),
    )


TAG = {"Key": "recipe", "Value": "cdc-to-lake"}   # clean_up.py deletes only roles that carry this tag


def role_names(s: Settings) -> dict:
    return {"lambda": f"{s.prefix}-cdc-lambda-role", "firehose": f"{s.prefix}-cdc-firehose-role",
            "scheduler": f"{s.prefix}-cdc-scheduler-role"}


def role_policies(s: Settings, account: str, stream_arn: str) -> dict:
    """What each role may do: these actions on these resources, nothing else (README, Decisions: "Roles")."""
    r, bucket = s.region, s.bucket(account)
    glue = [f"arn:aws:glue:{r}:{account}:catalog", f"arn:aws:glue:{r}:{account}:database/{s.database}",
            f"arn:aws:glue:{r}:{account}:table/{s.database}/*"]
    s3 = [f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*"]
    return {
        "lambda": [
            {"Effect": "Allow", "Resource": stream_arn,
             "Action": ["dynamodb:GetRecords", "dynamodb:GetShardIterator", "dynamodb:DescribeStream",
                        "dynamodb:ListStreams"]},
            {"Effect": "Allow", "Action": ["firehose:PutRecordBatch"],
             "Resource": f"arn:aws:firehose:{r}:{account}:deliverystream/{s.delivery_stream}"},
            {"Effect": "Allow", "Action": ["sqs:SendMessage"],
             "Resource": f"arn:aws:sqs:{r}:{account}:{s.failures_queue}"},
            {"Effect": "Allow", "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
             "Resource": f"arn:aws:logs:{r}:{account}:log-group:/aws/lambda/{s.function}:*"}],
        # Firehose docs, "Grant Firehose access to an Apache Iceberg Tables destination"
        "firehose": [
            {"Effect": "Allow", "Action": ["glue:GetTable", "glue:GetDatabase", "glue:UpdateTable"], "Resource": glue},
            {"Effect": "Allow", "Resource": s3,
             "Action": ["s3:AbortMultipartUpload", "s3:GetBucketLocation", "s3:GetObject", "s3:ListBucket",
                        "s3:ListBucketMultipartUploads", "s3:PutObject", "s3:DeleteObject"]}],
        "scheduler": [
            {"Effect": "Allow", "Action": ["athena:StartQueryExecution", "athena:GetQueryExecution"],
             "Resource": f"arn:aws:athena:{r}:{account}:workgroup/{s.workgroup}"},
            {"Effect": "Allow", "Action": ["glue:GetTable", "glue:GetDatabase", "glue:UpdateTable"], "Resource": glue},
            {"Effect": "Allow", "Resource": s3,
             "Action": ["s3:GetBucketLocation", "s3:GetObject", "s3:ListBucket", "s3:PutObject", "s3:DeleteObject"]}],
    }


SERVICE = {"lambda": "lambda.amazonaws.com", "firehose": "firehose.amazonaws.com",
           "scheduler": "scheduler.amazonaws.com"}
ENV = {"lambda": "lambda_role_arn", "firehose": "firehose_role_arn", "scheduler": "scheduler_role_arn"}


def ensure_roles(iam, s: Settings, account: str, stream_arn: str, which=("lambda", "firehose"),
                 sleep=time.sleep) -> Settings:
    """Create the roles that were not passed in; returns Settings with every role ARN filled."""
    names, policies, made, fresh = role_names(s), role_policies(s, account, stream_arn), {}, False
    for k in which:
        if getattr(s, ENV[k]):
            continue                                  # passed in by ARN: use it, create nothing
        trust = {"Version": "2012-10-17", "Statement": [
            {"Effect": "Allow", "Principal": {"Service": SERVICE[k]}, "Action": "sts:AssumeRole"}]}
        try:
            arn = iam.create_role(RoleName=names[k], AssumeRolePolicyDocument=json.dumps(trust),
                                  Tags=[TAG])["Role"]["Arn"]
            fresh = True
        except iam.exceptions.EntityAlreadyExistsException:
            arn = iam.get_role(RoleName=names[k])["Role"]["Arn"]
        iam.put_role_policy(RoleName=names[k], PolicyName="least-privilege",
                            PolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": policies[k]}))
        made[ENV[k]] = arn
    if fresh:
        sleep(10)       # a new role takes a few seconds before a service can assume it (assumed: 10 s)
    return dataclasses.replace(s, **made)


def create_all(s: Settings) -> dict:
    account = account_id(s.region)
    c = lambda svc: boto3.client(svc, region_name=s.region)  # noqa: E731
    stream_arn = create_table(c("dynamodb"), s)
    s = ensure_roles(c("iam"), s, account, stream_arn)
    create_bucket(c("s3"), s.bucket(account), s.region, own=not s.bucket_env)
    create_lake_table(c("glue"), c("athena"), s, account)
    create_firehose(c("firehose"), s, account)
    failures_arn = create_failures_queue(c("sqs"), s)
    create_function(c("lambda"), s, stream_arn, failures_arn)
    return {"account": account, "stream_arn": stream_arn, "bucket": s.bucket(account),
            "lambda_role": s.lambda_role_arn, "firehose_role": s.firehose_role_arn}


def main() -> None:
    import argparse

    p = argparse.ArgumentParser(description="Create the pipeline, or deploy a changed main/handler.py")
    p.add_argument("--update-code", action="store_true", help="only replace the Lambda function's code")
    args = p.parse_args()
    s = Settings()
    if args.update_code:
        boto3.client("lambda", region_name=s.region).update_function_code(
            FunctionName=s.function, ZipFile=function_zip())
        print(f"updated {s.function}")
    else:
        print(create_all(s))


if __name__ == "__main__":
    main()

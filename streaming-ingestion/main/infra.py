"""Create the AWS resources of the recipe with boto3. The same calls run against moto in the tests.

Every name starts with the prefix (PREFIX, default "recipe"), so teammates in one account do not collide and
clean_up.py can find everything. Settings and their reasons are in README "Decisions".
"""

import io
import json
import os
import time
import zipfile
from pathlib import Path

from botocore.exceptions import ClientError

RUNTIME = "python3.12"
ARCH = "arm64"            # Graviton: lower price per GB-second than x86 for the same memory (Lambda pricing page)
TAGS = {"purpose": "recipe-streaming-ingestion"}


def names(prefix: str) -> dict:
    p = prefix
    return {
        "stream": f"{p}-updates",
        "table": f"{p}-updates",
        "consumer": f"{p}-consumer",
        "consumer_failures": f"{p}-consumer-failures",
        # ext-windowed-aggregates
        "windows_table": f"{p}-windows",
        "windows": f"{p}-windows",
        "windows_failures": f"{p}-windows-failures",
        # ext-fan-out
        "topic": f"{p}-updates-topic",
        "dashboard": f"{p}-dashboard",
        "history": f"{p}-history",
        # ext-queue-ingest
        "trip_queue": f"{p}-trip-completed",
        "trip_dlq": f"{p}-trip-completed-dlq",
        "trip_consumer": f"{p}-trip-completed-consumer",
        "trip_consumer_role": f"{p}-queue-consumer-role",
        "trips_table": f"{p}-trips-completed",
        # ext-source-poller
        "poller": f"{p}-feed-poller",
        "poller_role": f"{p}-feed-poller-role",
        "scheduler_role": f"{p}-feed-scheduler-role",
        "slots_table": f"{p}-feed-slots",
        "schedule_group": f"{p}-source-poller",
        "schedule": f"{p}-feed-schedule",
    }


# ---------- Kinesis

def create_stream(kinesis, name: str, shards: int = 1, poll_s: float = 2.0) -> str:
    """Provisioned mode with one shard: a recipe run sends far below one shard's 1,000 records a second.

    A stream with this name that is still being deleted (a run right after a clean-up) is waited out and then
    created again: "in use" does not mean "there to use".
    """
    for _ in range(2):
        try:
            kinesis.create_stream(StreamName=name, ShardCount=shards,
                                  StreamModeDetails={"StreamMode": "PROVISIONED"})
            break
        except kinesis.exceptions.ResourceInUseException:
            try:
                status = kinesis.describe_stream_summary(StreamName=name)["StreamDescriptionSummary"]["StreamStatus"]
            except kinesis.exceptions.ResourceNotFoundException:
                continue                        # the deletion finished between the two calls: create it now
            if status != "DELETING":
                break                           # it exists (ACTIVE, or CREATING by a run a moment ago): use it
            kinesis.get_waiter("stream_not_exists").wait(
                StreamName=name, WaiterConfig={"Delay": poll_s, "MaxAttempts": 90})
    kinesis.get_waiter("stream_exists").wait(StreamName=name, WaiterConfig={"Delay": poll_s, "MaxAttempts": 60})
    return kinesis.describe_stream_summary(StreamName=name)["StreamDescriptionSummary"]["StreamARN"]


# ---------- DynamoDB

def create_table(ddb, name: str, key: str, poll_s: float = 2.0, sort_key: str | None = None,
                 tags: dict | None = None) -> str:
    """On-demand (pay per request): no capacity to size for a short run, and no charge while idle except storage.

    A table with this name that is still being deleted is waited out and then created again.
    """
    for _ in range(2):
        try:
            keys = [(key, "HASH")] + ([(sort_key, "RANGE")] if sort_key else [])
            ddb.create_table(TableName=name, BillingMode="PAY_PER_REQUEST",
                             KeySchema=[{"AttributeName": k, "KeyType": t} for k, t in keys],
                             AttributeDefinitions=[{"AttributeName": k, "AttributeType": "S"} for k, _ in keys],
                             Tags=[{"Key": k, "Value": v} for k, v in (tags or TAGS).items()])
            break
        except ddb.exceptions.ResourceInUseException:
            try:
                status = ddb.describe_table(TableName=name)["Table"]["TableStatus"]
            except ddb.exceptions.ResourceNotFoundException:
                continue                        # the deletion finished between the two calls: create it now
            if status != "DELETING":
                break
            ddb.get_waiter("table_not_exists").wait(TableName=name, WaiterConfig={"Delay": poll_s, "MaxAttempts": 90})
    ddb.get_waiter("table_exists").wait(TableName=name, WaiterConfig={"Delay": poll_s, "MaxAttempts": 90})
    return ddb.describe_table(TableName=name)["Table"]["TableArn"]


def expire_items(ddb, table: str, attribute: str) -> None:
    """Turn on time to live, unless it is on already: DynamoDB refuses to enable it twice (a second run)."""
    now = ddb.describe_time_to_live(TableName=table)["TimeToLiveDescription"]
    if now.get("TimeToLiveStatus") in ("ENABLED", "ENABLING") and now.get("AttributeName") == attribute:
        return
    ddb.update_time_to_live(TableName=table, TimeToLiveSpecification={"Enabled": True, "AttributeName": attribute})


# ---------- SQS

FAILED_KEEP_DAYS = 14      # a queue of failures keeps its messages 14 days, the longest SQS allows


def create_queue(sqs, name: str, dead_letter_arn: str | None = None, max_receives: int = 5,
                 recreate_wait_s: float = 90.0, poll_s: float = 5.0, keep_days: int = 4,
                 visibility_s: int = 180, tags: dict | None = None) -> tuple[str, str]:
    """A consumer's queue keeps a message 4 days (the SQS default). Pass keep_days=FAILED_KEEP_DAYS for a queue that
    holds failures: a message in a dead-letter queue expires by the time it was first sent, so with the same 4 days
    a message that failed on day 3 would be gone a day later, before anyone has looked."""
    attrs = {"MessageRetentionPeriod": str(keep_days * 24 * 3600)}
    if dead_letter_arn:
        # a message that fails max_receives times moves to the dead-letter queue instead of blocking the queue
        attrs["RedrivePolicy"] = json.dumps({"deadLetterTargetArn": dead_letter_arn, "maxReceiveCount": max_receives})
        attrs["VisibilityTimeout"] = str(visibility_s)   # 6 x the function timeout, plus a batching window (Lambda SQS docs)
    end = time.time() + recreate_wait_s
    while True:
        try:
            url = sqs.create_queue(QueueName=name, Attributes=attrs, tags=tags or TAGS)["QueueUrl"]
            break
        except ClientError as err:
            code = err.response["Error"]["Code"]
            if code.endswith(("QueueAlreadyExists", "QueueNameExists")):
                # a second run with other settings (another batching window): the queue is given this run's
                url = sqs.get_queue_url(QueueName=name)["QueueUrl"]
                sqs.set_queue_attributes(QueueUrl=url, Attributes=attrs)
                break
            # SQS refuses a name for 60 s after a queue with it was deleted (a run right after a clean-up)
            if not code.endswith("QueueDeletedRecently") or time.time() >= end:
                raise
            time.sleep(poll_s)
    arn = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
    return url, arn


# ---------- IAM and Lambda

LAMBDA_TRUST = {"Version": "2012-10-17", "Statement": [{
    "Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}]}


def create_role(iam, name: str, statements: list[dict], region: str, account: str, function: str,
                tags: dict | None = None) -> str:
    """A role for one function, allowed only what that function does, plus writing its own logs (SEC03-BP02).

    To use a role you already have instead, set LAMBDA_ROLE_ARN: every function then runs with that role and none
    is created (README, Run it). The role must allow what each function does (the statements below).
    """
    if os.environ.get("LAMBDA_ROLE_ARN"):
        return os.environ["LAMBDA_ROLE_ARN"]
    logs = {"Effect": "Allow", "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
            "Resource": f"arn:aws:logs:{region}:{account}:log-group:/aws/lambda/{function}:*"}
    try:
        arn = iam.create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps(LAMBDA_TRUST),
                              Tags=[{"Key": k, "Value": v} for k, v in (tags or TAGS).items()])["Role"]["Arn"]
    except iam.exceptions.EntityAlreadyExistsException:
        arn = iam.get_role(RoleName=name)["Role"]["Arn"]
    iam.put_role_policy(RoleName=name, PolicyName="least-privilege",
                        PolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": statements + [logs]}))
    return arn


LOG_DAYS = 7               # a function's log lines are kept a week (ours)


def create_log_group(logs, function: str, tags: dict | None = None) -> None:
    """The function's log group, made before the function, with a retention. Lambda would make the group itself on
    the first call, and that group keeps every line until somebody deletes it."""
    name = f"/aws/lambda/{function}"
    try:
        logs.create_log_group(logGroupName=name, tags=tags or TAGS)
    except logs.exceptions.ResourceAlreadyExistsException:
        pass
    logs.put_retention_policy(logGroupName=name, retentionInDays=LOG_DAYS)


def zip_files(files: dict[str, Path]) -> bytes:
    """A deployment package: each file at the top level of the zip, as the handler imports it."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for arcname, path in files.items():
            z.write(path, arcname)
    return buf.getvalue()


def create_function(lam, name: str, role_arn: str, package: bytes, handler: str, env: dict,
                    timeout: int = 30, memory: int = 128, role_wait_s: float = 3.0, tags: dict | None = None) -> str:
    for attempt in range(10):
        try:
            arn = lam.create_function(
                FunctionName=name, Runtime=RUNTIME, Architectures=[ARCH], Role=role_arn, Handler=handler,
                Code={"ZipFile": package}, Timeout=timeout, MemorySize=memory,
                Environment={"Variables": env}, Tags=tags or TAGS)["FunctionArn"]
            break
        except lam.exceptions.ResourceConflictException:
            lam.update_function_code(FunctionName=name, ZipFile=package)
            arn = lam.get_function(FunctionName=name)["Configuration"]["FunctionArn"]
            break
        except lam.exceptions.InvalidParameterValueException:
            time.sleep(role_wait_s)             # a role made seconds ago may not be usable by Lambda yet
    else:
        raise RuntimeError(f"could not create {name}")
    lam.get_waiter("function_active_v2").wait(FunctionName=name)
    return arn


def create_mapping(lam, function: str, source_arn: str, **settings) -> str:
    """Event source mapping: Lambda polls the source and calls the function with batches. Returns its UUID.

    Lambda refuses a second mapping of the same source to the same function (a second run after --keep), so a
    mapping that is already there is given these settings and used. Lambda refuses an update while the mapping is
    still being created or changed, so the update waits until it is Enabled or Disabled.
    """
    found = [m for m in lam.list_event_source_mappings(FunctionName=function, EventSourceArn=source_arn)[
        "EventSourceMappings"] if m.get("State") != "Deleting"]
    if not found:
        return lam.create_event_source_mapping(FunctionName=function, EventSourceArn=source_arn, **settings)["UUID"]
    settings.pop("StartingPosition", None)            # fixed when a mapping is created; an update does not take it
    wait_mapping(lam, found[0]["UUID"], state=("Enabled", "Disabled"))
    lam.update_event_source_mapping(UUID=found[0]["UUID"], FunctionName=function, **settings)
    return found[0]["UUID"]


def kinesis_mapping(lam, function: str, stream_arn: str, failures_arn: str, batch_size: int = 100) -> str:
    return create_mapping(
        lam, function, stream_arn,
        StartingPosition="TRIM_HORIZON",              # read from the oldest record kept, so nothing sent early is missed
        BatchSize=batch_size,
        MaximumBatchingWindowInSeconds=1,             # wait at most 1 s to fill a batch: adds up to 1 s to the lag
        FunctionResponseTypes=["ReportBatchItemFailures"],
        BisectBatchOnFunctionError=True,              # split a failing batch to find the bad record
        MaximumRetryAttempts=3,                       # default is -1: retry until the record expires and block the shard
        MaximumRecordAgeInSeconds=3600,
        DestinationConfig={"OnFailure": {"Destination": failures_arn}},   # what was given up is recorded, not lost
    )


def wait_mapping(lam, uuid: str, state: str | tuple[str, ...] = "Enabled", timeout_s: int = 180):
    """Wait until the mapping is in `state` (one state, or any of several)."""
    states = (state,) if isinstance(state, str) else state
    end = time.time() + timeout_s
    while time.time() < end:
        if lam.get_event_source_mapping(UUID=uuid)["State"] in states:
            return
        time.sleep(3)
    raise TimeoutError(f"event source mapping {uuid} not {' or '.join(states)} after {timeout_s} s")


def account_id(session) -> str:
    return session.client("sts").get_caller_identity()["Account"]


# ---------- the main scenario's resources

KINESIS_READ = ["kinesis:GetRecords", "kinesis:GetShardIterator", "kinesis:DescribeStream",
                "kinesis:DescribeStreamSummary", "kinesis:ListShards", "kinesis:ListStreams"]


def deploy_main(session, prefix: str) -> dict:
    n = names(prefix)
    region = session.region_name
    acct = account_id(session)
    kinesis, ddb, sqs = session.client("kinesis"), session.client("dynamodb"), session.client("sqs")
    iam, lam = session.client("iam"), session.client("lambda")

    stream_arn = create_stream(kinesis, n["stream"])
    table_arn = create_table(ddb, n["table"], "update_id")
    _, failures_arn = create_queue(sqs, n["consumer_failures"], keep_days=FAILED_KEEP_DAYS)
    role_arn = create_role(iam, f"{n['consumer']}-role", [
        {"Effect": "Allow", "Action": KINESIS_READ, "Resource": stream_arn},
        {"Effect": "Allow", "Action": ["dynamodb:PutItem"], "Resource": table_arn},
        {"Effect": "Allow", "Action": ["sqs:SendMessage"], "Resource": failures_arn},
    ], region, acct, n["consumer"])
    here = Path(__file__).parent
    create_log_group(session.client("logs"), n["consumer"])
    create_function(lam, n["consumer"], role_arn, zip_files({"consumer.py": here / "consumer.py"}),
                    "consumer.handler", {"TABLE": n["table"]})
    uuid = kinesis_mapping(lam, n["consumer"], stream_arn, failures_arn)
    return {"stream_arn": stream_arn, "table_arn": table_arn, "mapping": uuid}

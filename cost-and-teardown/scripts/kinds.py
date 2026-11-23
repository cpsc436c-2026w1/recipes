"""What the inventory and the teardown know about each kind of resource: its name in words, how it bills, how its
ARN reads, how its own service is asked for it, and how it is deleted.

A kind with no `delete` is listed under "Delete by hand" when the teardown finds it. `exists` asks the resource's
own service by name and returns False only on that service's "not found" (any other error is raised): the Tagging
API also returns resources that were tagged once, so its list is a list of candidates, not proof.

Hourly kinds bill while they exist, used or not (price sheet, "How to use the sheet"); monthly kinds are priced for
the days they exist; every other kind bills per use.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from botocore.exceptions import ClientError

REGION = "ca-central-1"
HOURLY = ("kinesis-stream", "emr-cluster", "ec2-instance", "public-ipv4", "nat-gateway", "load-balancer",
          "ecs-service", "lambda-provisioned-concurrency", "rds-instance", "opensearch", "sagemaker-endpoint",
          "msk-cluster")
MONTHLY = ("s3-bucket", "dynamodb-table", "ebs-volume", "cloudwatch-alarm", "custom-metric", "secret", "kms-key")


def bills(kind: str) -> str:
    return "hourly" if kind in HOURLY else "monthly" if kind in MONTHLY else "per use"


def _gone(err: ClientError, *codes: str) -> bool:
    return err.response["Error"]["Code"] in codes or err.response["ResponseMetadata"].get("HTTPStatusCode") == 404


@dataclass
class Kind:
    label: str
    exists: Callable | None = None       # (sess, name) -> bool
    delete: Callable | None = None       # (sess, name) -> None
    asks: str = ""                       # the operation that asks for it, for the record


def _ask(fn, *codes):
    def exists(sess, name):
        try:
            fn(sess, name)
            return True
        except ClientError as err:
            if _gone(err, *codes):
                return False
            raise
    return exists


def _bucket_exists(sess, name):
    return _ask(lambda s, n: s.client("s3").head_bucket(Bucket=n), "404", "NoSuchBucket", "NotFound")(sess, name)


def _delete_bucket(sess, name):
    s3 = sess.client("s3")
    for page in s3.get_paginator("list_object_versions").paginate(Bucket=name):
        objs = [{"Key": o["Key"], "VersionId": o["VersionId"]}
                for o in page.get("Versions", []) + page.get("DeleteMarkers", [])]
        for i in range(0, len(objs), 1000):
            s3.delete_objects(Bucket=name, Delete={"Objects": objs[i:i + 1000], "Quiet": True})
    s3.delete_bucket(Bucket=name)


def _stream_exists(sess, name):
    try:
        status = sess.client("kinesis").describe_stream_summary(StreamName=name)["StreamDescriptionSummary"]
        return status["StreamStatus"] != "DELETING"
    except ClientError as err:
        if _gone(err, "ResourceNotFoundException"):
            return False
        raise


def _table_exists(sess, name):
    try:
        status = sess.client("dynamodb").describe_table(TableName=name)["Table"]["TableStatus"]
        return status != "DELETING"
    except ClientError as err:
        if _gone(err, "ResourceNotFoundException"):
            return False
        raise


def _esm_uuid(name: str) -> str:
    return name.rsplit(":", 1)[-1]


def _log_group_exists(sess, name):
    groups = sess.client("logs").describe_log_groups(logGroupNamePrefix=name).get("logGroups", [])
    return any(g["logGroupName"] == name for g in groups)


def _workgroup_exists(sess, name):
    athena, token = sess.client("athena"), None
    while True:                       # ListWorkGroups has no boto3 paginator
        page = athena.list_work_groups(**({"NextToken": token} if token else {}))
        if any(w["Name"] == name for w in page["WorkGroups"]):
            return True
        token = page.get("NextToken")
        if not token:
            return False


def _role_delete(sess, name):
    iam = sess.client("iam")
    for p in iam.list_role_policies(RoleName=name)["PolicyNames"]:
        iam.delete_role_policy(RoleName=name, PolicyName=p)
    for p in iam.list_attached_role_policies(RoleName=name)["AttachedPolicies"]:
        iam.detach_role_policy(RoleName=name, PolicyArn=p["PolicyArn"])
    iam.delete_role(RoleName=name)


def _glue_table(name):
    db, _, table = name.partition(".")
    return db, table


def _schedule_group_exists(sess, name):
    try:
        state = sess.client("scheduler").get_schedule_group(Name=name).get("State")
        return state != "DELETING"
    except ClientError as err:
        if _gone(err, "ResourceNotFoundException"):
            return False
        raise


def _account(sess) -> str:
    return sess.client("sts").get_caller_identity()["Account"]


def _topic_arn(sess, name):
    return name if name.startswith("arn:") else f"arn:aws:sns:{REGION}:{_account(sess)}:{name}"


def _schedule(name):
    group, _, schedule = name.partition("/")
    return {"GroupName": group, "Name": schedule}


def _notification_bucket(sess) -> str:
    """The lake bucket a recipe's notification entry is on: `<PREFIX>-lake-<account>` (scripts/lake.py)."""
    from tags import prefix
    return f"{prefix()}-lake-{_account(sess)}"


def _notification_exists(sess, name):
    s3 = sess.client("s3")
    try:
        config = s3.get_bucket_notification_configuration(Bucket=_notification_bucket(sess))
    except ClientError as err:
        if _gone(err, "NoSuchBucket", "404"):
            return False
        raise
    return any(e.get("Id") == name for kind in ("LambdaFunctionConfigurations", "QueueConfigurations",
                                               "TopicConfigurations") for e in config.get(kind, []))


def _notification_delete(sess, name):
    """Drop the entry by its Id and write the rest back: PutBucketNotificationConfiguration replaces them all."""
    s3, bucket = sess.client("s3"), _notification_bucket(sess)
    config = s3.get_bucket_notification_configuration(Bucket=bucket)
    config.pop("ResponseMetadata", None)
    for kind in ("LambdaFunctionConfigurations", "QueueConfigurations", "TopicConfigurations"):
        config[kind] = [e for e in config.get(kind, []) if e.get("Id") != name]
    s3.put_bucket_notification_configuration(Bucket=bucket,
                                             NotificationConfiguration={k: v for k, v in config.items() if v})


def _queue_url(sess, name):
    return sess.client("sqs").get_queue_url(QueueName=name)["QueueUrl"]


KINDS: dict[str, Kind] = {
    "s3-bucket": Kind("S3 bucket", _bucket_exists, _delete_bucket, "HeadBucket"),
    "kinesis-stream": Kind("Kinesis data stream", _stream_exists,
                           lambda s, n: s.client("kinesis").delete_stream(StreamName=n, EnforceConsumerDeletion=True),
                           "DescribeStreamSummary"),
    "lambda-function": Kind("Lambda function",
                            _ask(lambda s, n: s.client("lambda").get_function(FunctionName=n),
                                 "ResourceNotFoundException"),
                            lambda s, n: s.client("lambda").delete_function(FunctionName=n), "GetFunction"),
    "lambda-esm": Kind("Lambda event source mapping",
                       _ask(lambda s, n: s.client("lambda").get_event_source_mapping(UUID=_esm_uuid(n)),
                            "ResourceNotFoundException"),
                       lambda s, n: s.client("lambda").delete_event_source_mapping(UUID=_esm_uuid(n)),
                       "GetEventSourceMapping"),
    "log-group": Kind("CloudWatch log group", _log_group_exists,
                      lambda s, n: s.client("logs").delete_log_group(logGroupName=n), "DescribeLogGroups"),
    "dynamodb-table": Kind("DynamoDB table", _table_exists,
                           lambda s, n: s.client("dynamodb").delete_table(TableName=n), "DescribeTable"),
    "athena-workgroup": Kind("Athena workgroup", _workgroup_exists,
                             lambda s, n: s.client("athena").delete_work_group(WorkGroup=n, RecursiveDeleteOption=True),
                             "ListWorkGroups"),
    "athena-data-catalog": Kind("Athena data source",
                                _ask(lambda s, n: s.client("athena").get_data_catalog(Name=n), "InvalidRequestException"),
                                None, "GetDataCatalog"),
    "iam-role": Kind("IAM role", _ask(lambda s, n: s.client("iam").get_role(RoleName=n), "NoSuchEntity"),
                     _role_delete, "GetRole"),
    "glue-database": Kind("Glue database",
                          _ask(lambda s, n: s.client("glue").get_database(Name=n), "EntityNotFoundException"),
                          lambda s, n: s.client("glue").delete_database(Name=n), "GetDatabase"),
    "glue-table": Kind("Glue table",
                       _ask(lambda s, n: s.client("glue").get_table(DatabaseName=_glue_table(n)[0],
                                                                    Name=_glue_table(n)[1]), "EntityNotFoundException"),
                       lambda s, n: s.client("glue").delete_table(DatabaseName=_glue_table(n)[0],
                                                                  Name=_glue_table(n)[1]), "GetTable"),
    "sqs-queue": Kind("SQS queue",
                      _ask(_queue_url, "AWS.SimpleQueueService.NonExistentQueue", "QueueDoesNotExist"),
                      lambda s, n: s.client("sqs").delete_queue(QueueUrl=_queue_url(s, n)), "GetQueueUrl"),
    "schedule-group": Kind("Scheduler schedule group", _schedule_group_exists,
                           lambda s, n: s.client("scheduler").delete_schedule_group(Name=n), "GetScheduleGroup"),
    "cloudwatch-alarm": Kind("CloudWatch alarm",
                             lambda s, n: bool(s.client("cloudwatch").describe_alarms(AlarmNames=[n])["MetricAlarms"]),
                             lambda s, n: s.client("cloudwatch").delete_alarms(AlarmNames=[n]), "DescribeAlarms"),
    # the kinds below were "delete by hand" until the live teardown of s3-event-etl (2026-11-23) found them
    "sns-topic": Kind("SNS topic",
                      _ask(lambda s, n: s.client("sns").get_topic_attributes(TopicArn=_topic_arn(s, n)), "NotFound"),
                      lambda s, n: s.client("sns").delete_topic(TopicArn=_topic_arn(s, n)), "GetTopicAttributes"),
    "glue-job": Kind("Glue job", _ask(lambda s, n: s.client("glue").get_job(JobName=n), "EntityNotFoundException"),
                     lambda s, n: s.client("glue").delete_job(JobName=n), "GetJob"),
    "schedule": Kind("Scheduler schedule",
                     _ask(lambda s, n: s.client("scheduler").get_schedule(**_schedule(n)), "ResourceNotFoundException"),
                     lambda s, n: s.client("scheduler").delete_schedule(**_schedule(n)), "GetSchedule"),
    "s3-notification": Kind("S3 notification entry", _notification_exists, _notification_delete,
                            "GetBucketNotificationConfiguration"),
}


def label(kind: str) -> str:
    return KINDS[kind].label if kind in KINDS else kind


def from_arn(arn: str) -> tuple[str, str]:
    """(kind, name) of an ARN; an unknown service gives "<service>:<resource type>" and the ARN's last part."""
    parts = arn.split(":", 5)
    service, rest = parts[2], parts[5]
    if service == "s3":
        return "s3-bucket", rest
    if service == "kinesis" and rest.startswith("stream/"):
        return "kinesis-stream", rest.split("/", 1)[1]
    if service == "lambda" and rest.startswith("function:"):
        return "lambda-function", rest.split(":")[1]
    if service == "lambda" and rest.startswith("event-source-mapping:"):
        return "lambda-esm", rest.split(":")[1]
    if service == "logs" and rest.startswith("log-group:"):
        return "log-group", rest.split(":", 1)[1].removesuffix(":*")
    if service == "dynamodb" and rest.startswith("table/"):
        return "dynamodb-table", rest.split("/")[1]
    if service == "athena" and rest.startswith("workgroup/"):
        return "athena-workgroup", rest.split("/", 1)[1]
    if service == "athena" and rest.startswith("datacatalog/"):
        return "athena-data-catalog", rest.split("/", 1)[1]
    if service == "iam" and rest.startswith("role/"):
        return "iam-role", rest.rsplit("/", 1)[1]
    if service == "glue" and rest.startswith("database/"):
        return "glue-database", rest.split("/", 1)[1]
    if service == "glue" and rest.startswith("table/"):
        _, db, table = rest.split("/", 2)
        return "glue-table", f"{db}.{table}"
    if service == "sqs":
        return "sqs-queue", rest
    if service == "scheduler" and rest.startswith("schedule-group/"):
        return "schedule-group", rest.split("/", 1)[1]
    if service == "cloudwatch" and rest.startswith("alarm:"):
        return "cloudwatch-alarm", rest.split(":", 1)[1]
    if service == "sns":
        return "sns-topic", rest
    if service == "elasticloadbalancing" and rest.startswith("loadbalancer/app/"):
        return "load-balancer", rest.split("/")[2]
    if service == "glue" and rest.startswith("job/"):
        return "glue-job", rest.split("/", 1)[1]
    if service == "scheduler" and rest.startswith("schedule/"):
        return "schedule", rest.split("/", 1)[1]
    kind = rest.split("/")[0].split(":")[0]
    return f"{service}:{kind}", rest.rsplit("/", 1)[-1]


def arn_of(kind: str, name: str, account: str) -> str:
    """The ARN of a kind and name (the ledger's key when a row has no ARN)."""
    r, a = REGION, account
    return {
        "s3-bucket": f"arn:aws:s3:::{name}",
        "kinesis-stream": f"arn:aws:kinesis:{r}:{a}:stream/{name}",
        "lambda-function": f"arn:aws:lambda:{r}:{a}:function:{name}",
        "log-group": f"arn:aws:logs:{r}:{a}:log-group:{name}",
        "dynamodb-table": f"arn:aws:dynamodb:{r}:{a}:table/{name}",
        "athena-workgroup": f"arn:aws:athena:{r}:{a}:workgroup/{name}",
        "athena-data-catalog": f"arn:aws:athena:{r}:{a}:datacatalog/{name}",
        "iam-role": f"arn:aws:iam::{a}:role/{name}",
        "glue-database": f"arn:aws:glue:{r}:{a}:database/{name}",
        "glue-table": f"arn:aws:glue:{r}:{a}:table/{name.replace('.', '/', 1)}",
        "sqs-queue": f"arn:aws:sqs:{r}:{a}:{name}",
        "schedule-group": f"arn:aws:scheduler:{r}:{a}:schedule-group/{name}",
        "cloudwatch-alarm": f"arn:aws:cloudwatch:{r}:{a}:alarm:{name}",
        "sns-topic": f"arn:aws:sns:{r}:{a}:{name}",
        "glue-job": f"arn:aws:glue:{r}:{a}:job/{name}",
        "schedule": f"arn:aws:scheduler:{r}:{a}:schedule/{name}",
    }.get(kind, f"{kind}:{name}")


def canonical(arn: str) -> str:
    """One form per resource: the log group ARN without its :* and a role ARN without its path."""
    kind, name = from_arn(arn)
    if kind in ("log-group", "iam-role"):
        return arn_of(kind, name, arn.split(":")[4])
    return arn

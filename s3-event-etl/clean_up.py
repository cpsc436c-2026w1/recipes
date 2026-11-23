"""Remove what scripts/etl.py up made, the bucket's trigger entry first, and nothing else.

    uv run python clean_up.py            # what it would delete; nothing is deleted (PREFIX exported first)
    uv run python clean_up.py --yes      # delete it

Order: the notification entry <PREFIX>-s3-event-etl (read the configuration, drop that Id, write the rest back), so
no event reaches a deleted function; the schedule group (its schedule goes with it); both functions and their log
groups; the Glue job; the roles; the topic; the run log's items of jobs etl, etl-object and etl-lock (the table only
when no other job has items and this recipe created it); the tables trip_events_clean and trip_events_stage; the
prefixes stage/trip-events/, quarantine/trip-events-rows/, lake/etl-state/, lake/trip_events_clean/ and
jobs/s3-event-etl/. It keeps the bucket, raw/trip-events/ and the workgroup (scripts/lake.py down deletes them).
Every step is attempted; a refused one is printed FAILED and the exit code is 1. Running it twice is safe.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError

ROOT = Path(__file__).resolve().parent
for folder in (ROOT / "scripts", ROOT / "pipeline"):
    sys.path.insert(0, str(folder))

from etl import names, remove_entry  # noqa: E402
from lake import bucket_name  # noqa: E402
from tags import record, recorded  # noqa: E402
import control  # noqa: E402

PREFIXES = ("stage/trip-events/", "quarantine/trip-events-rows/", "lake/etl-state/", "lake/trip_events_clean/",
            "jobs/s3-event-etl/")
GONE = {"ResourceNotFoundException", "NoSuchEntity", "EntityNotFoundException", "NotFound", "NoSuchBucket", "404",
        "NotFoundException"}


class Steps:
    def __init__(self, yes: bool, log=print):
        self.yes, self.log, self.failed = yes, log, 0

    def do(self, what: str, fn, *a, ledger: tuple[str, str] | None = None, exists=None, **kw) -> None:
        """One delete; with `ledger` = (kind, name), a `created` row of the ledger is followed by `deleted` once the
        delete went through or found it gone (the live teardown of 2026-11-23 found rows still `created` for tables
        and roles this clean-up had deleted). `exists` asks first: Glue DeleteJob and SNS DeleteTopic succeed on a
        name that is not there, so without it a gone resource was printed "removed"."""
        try:
            if exists is not None and not exists():
                self.log(f"already gone {what}")
                if self.yes and ledger and recorded(*ledger) == "created":
                    record(*ledger, "deleted", script="clean_up.py")
                return
            if self.yes:
                fn(*a, **kw)
            self.log(f"{'removed' if self.yes else 'would remove':<8} {what}")
        except (ClientError, BotoCoreError) as err:
            code = getattr(err, "response", {}).get("Error", {}).get("Code", type(err).__name__)
            if code in GONE:
                self.log(f"absent   {what}")
            else:
                self.failed += 1
                self.log(f"FAILED   {what}: {code}")
                return
        if self.yes and ledger and recorded(*ledger) == "created":
            record(*ledger, "deleted", script="clean_up.py")


def asked(fn, *a, **kw):
    """`exists` for Steps.do: True when the call answers, False on the service's "not found"."""
    def exists() -> bool:
        try:
            return fn(*a, **kw) is not False
        except (ClientError, BotoCoreError) as err:
            if getattr(err, "response", {}).get("Error", {}).get("Code") in GONE:
                return False
            raise
    return exists


def has_entry(s3, bucket: str, entry_id: str) -> bool:
    config = s3.get_bucket_notification_configuration(Bucket=bucket)
    return any(e.get("Id") == entry_id for kind in ("LambdaFunctionConfigurations", "QueueConfigurations",
                                                    "TopicConfigurations") for e in config.get(kind, []))


def has_objects(s3, bucket: str, prefix: str) -> bool:
    return s3.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=1).get("KeyCount", 0) > 0


def has_run_items(ddb, table: str) -> bool:
    return any(ddb.query(TableName=table, KeyConditionExpression="job = :j", Limit=1,
                         ExpressionAttributeValues={":j": {"S": job}})["Items"] for job in ("etl", "etl-object", "etl-lock"))


def log_group_exists(logs, name: str) -> bool:
    return any(g["logGroupName"] == name for g in logs.describe_log_groups(logGroupNamePrefix=name)["logGroups"])


def delete_prefix(s3, bucket: str, prefix: str) -> None:
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
        if keys:
            s3.delete_objects(Bucket=bucket, Delete={"Objects": keys, "Quiet": True})


def delete_run_items(ddb, table: str) -> None:
    for job in ("etl", "etl-object", "etl-lock"):
        start = None
        while True:
            page = ddb.query(TableName=table, KeyConditionExpression="job = :j",
                             ExpressionAttributeValues={":j": {"S": job}}, ProjectionExpression="job, run_id",
                             **({"ExclusiveStartKey": start} if start else {}))
            for it in page["Items"]:
                ddb.delete_item(TableName=table, Key={"job": it["job"], "run_id": it["run_id"]})
            start = page.get("LastEvaluatedKey")
            if not start:
                break


def delete_role(iam, name: str) -> None:
    for p in iam.list_role_policies(RoleName=name)["PolicyNames"]:
        iam.delete_role_policy(RoleName=name, PolicyName=p)
    for p in iam.list_attached_role_policies(RoleName=name)["AttachedPolicies"]:
        iam.detach_role_policy(RoleName=name, PolicyArn=p["PolicyArn"])
    iam.delete_role(RoleName=name)


def clean(sess=None, yes: bool = False, log=print) -> int:
    sess = sess or boto3.session.Session(region_name="ca-central-1")
    n, bucket = names(), bucket_name(sess)
    account = sess.client("sts").get_caller_identity()["Account"]
    s3, lam, glue, iam = sess.client("s3"), sess.client("lambda"), sess.client("glue"), sess.client("iam")
    logs, sns, ddb, sch = sess.client("logs"), sess.client("sns"), sess.client("dynamodb"), sess.client("scheduler")
    steps = Steps(yes, log)
    steps.do(f"notification entry {n['entry']} of {bucket} (the other entries kept)", remove_entry, s3, bucket,
             n["entry"], ledger=("s3-notification", n["entry"]), exists=asked(has_entry, s3, bucket, n["entry"]))
    steps.do(f"schedule group {n['group']} and its schedule", sch.delete_schedule_group, Name=n["group"],
             ledger=("schedule-group", n["group"]), exists=asked(sch.get_schedule_group, Name=n["group"]))
    for fn in (n["check"], n["sweep"]):
        steps.do(f"function {fn}", lam.delete_function, FunctionName=fn, ledger=("lambda-function", fn),
                 exists=asked(lam.get_function, FunctionName=fn))
        group = f"/aws/lambda/{fn}"
        steps.do(f"log group {group}", logs.delete_log_group, logGroupName=group, ledger=("log-group", group),
                 exists=asked(log_group_exists, logs, group))
    steps.do(f"Glue job {n['job']}", glue.delete_job, JobName=n["job"], ledger=("glue-job", n["job"]),
             exists=asked(glue.get_job, JobName=n["job"]))
    for role in n["roles"].values():
        steps.do(f"role {role}", delete_role, iam, role, ledger=("iam-role", role),
                 exists=asked(iam.get_role, RoleName=role))
    topic = f"arn:aws:sns:ca-central-1:{account}:{n['topic']}"
    steps.do(f"topic {n['topic']}", sns.delete_topic, TopicArn=topic, ledger=("sns-topic", n["topic"]),
             exists=asked(sns.get_topic_attributes, TopicArn=topic))
    runs = control.table()
    steps.do(f"run log items etl, etl-object, etl-lock of {runs}", delete_run_items, ddb, runs,
             exists=asked(has_run_items, ddb, runs))
    if recorded("dynamodb-table", runs) == "created":
        try:
            other = ddb.scan(TableName=runs, Select="COUNT", ConsistentRead=True)["Count"] if yes else 0
        except ClientError:
            other = 0
        if other:
            log(f"kept     table {runs}: {other} items of other jobs")
        else:
            steps.do(f"table {runs}", ddb.delete_table, TableName=runs, ledger=("dynamodb-table", runs),
                     exists=asked(ddb.describe_table, TableName=runs))
    for t in ("trip_events_clean", "trip_events_stage"):
        steps.do(f"Glue table {n['database']}.{t}", glue.delete_table, DatabaseName=n["database"], Name=t,
                 ledger=("glue-table", f"{n['database']}.{t}"),
                 exists=asked(glue.get_table, DatabaseName=n["database"], Name=t))
    for p in PREFIXES:
        steps.do(f"s3://{bucket}/{p}", delete_prefix, s3, bucket, p, exists=asked(has_objects, s3, bucket, p))
    log(f"kept     bucket {bucket}, raw/trip-events/ and the workgroup (scripts/lake.py down)")
    return 1 if steps.failed else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--yes", action="store_true")
    yes = ap.parse_args(argv).yes
    code = clean(yes=yes)
    if not yes:
        print("Nothing deleted. Run again with --yes to delete these.")
    return code


if __name__ == "__main__":
    sys.exit(main())

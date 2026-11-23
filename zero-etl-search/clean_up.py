"""Remove everything a real run of this recipe created in your AWS account, for this PREFIX.

  PREFIX=recipe-yourname uv run python clean_up.py            list only: nothing is deleted
  PREFIX=recipe-yourname uv run python clean_up.py --yes      delete

Removes, in this order: the OpenSearch Ingestion pipeline of ext-zero-etl (it bills per hour while it exists), the
indexing function and the re-embedding function (ext-embeddings), each with its event source mappings and its log
group, the failure queue and the ext-embeddings queues, both collections with their indexes, their three policies
each and the collection group, the S3 bucket of ext-zero-etl, the DynamoDB table with its stream, then the IAM roles
the recipe created (tagged recipe=zero-etl-search); a role passed in by ARN does not carry the tag and stays.

Every step is tried, also after one fails. Each line says what happened: removed, absent (it was already gone) or
FAILED with the reason. The exit code is 1 when a step failed. An account whose service control policy denies
OpenSearch Ingestion (a course sandbox does, output/main-aws-denied.txt) cannot have a pipeline: that step prints
"not checked (denied by a service control policy)" and does not change the exit code.
"""

from __future__ import annotations

import argparse
import sys
import time
from contextlib import contextmanager

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from main.config import Settings

# Error codes that mean "already gone": such a step is reported as absent, not as a failure.
GONE = {"ResourceNotFoundException", "ResourceNotFound", "NotFoundException", "NotFound", "404", "NoSuchEntity",
        "NoSuchBucket", "EntityNotFoundException", "QueueDoesNotExist", "AWS.SimpleQueueService.NonExistentQueue"}


class Steps:
    """Runs every clean-up step, also after one fails, and keeps what happened to each (README, Cost and clean-up).

    removed: deleted now (with dry_run: would be deleted). absent: was already gone. failed: the call was refused or
    broke; the text says why. A clean-up with anything in `failed` must exit non-zero, so nobody reads it as done.
    """

    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self.removed: list[str] = []
        self.absent: list[str] = []
        self.failed: list[str] = []

    def _note(self, what: str, err: Exception) -> None:
        code = getattr(err, "response", {}).get("Error", {}).get("Code", "")
        if code in GONE:
            self.absent.append(what)
        else:
            self.failed.append(f"{what}: {code or type(err).__name__}: {err}")

    def do(self, what: str, fn=None, **kw) -> bool:
        """One delete call. True when it went through (or would, with dry_run)."""
        try:
            if fn and not self.dry_run:
                fn(**kw)
        except (ClientError, BotoCoreError, TimeoutError) as err:
            self._note(what, err)
            return False
        self.removed.append(what)
        return True

    @contextmanager
    def part(self, what: str):
        """A group of look-ups and deletes for one service: an error inside ends this part only."""
        try:
            yield
        except (ClientError, BotoCoreError, TimeoutError) as err:
            self._note(what, err)

    def report(self, log=print) -> int:
        """Print one line per step; return the exit code (1 when any step failed)."""
        verb = "would remove" if self.dry_run else "removed"
        for kind, items in ((verb, self.removed), ("absent", self.absent), ("FAILED", self.failed)):
            for item in items:
                log(f"{kind:<8} {item}")
        return 1 if self.failed else 0


def _wait(check, what: str, wait_s: int) -> None:
    deadline = time.time() + wait_s
    while time.time() < deadline:
        if not check():
            return
        time.sleep(10)
    raise TimeoutError(f"{what} still there after {wait_s} s; run clean_up.py again")


def clean_up(s: Settings, dry_run: bool = False, session=None, wait_s: int = 900, aoss=None) -> Steps:
    session = session or boto3.Session(region_name=s.region)
    c = lambda svc: session.client(svc, region_name=s.region)  # noqa: E731
    steps = Steps(dry_run)

    steps.unchecked = []      # steps the account would not let us look at; printed, not counted as failures

    # ext-zero-etl: the pipeline. An account whose service control policy denies osis:* cannot have made one, so
    # that refusal is reported as "not checked" and does not fail the clean-up.
    name = f"pipeline {s.pipeline}"
    try:
        osis = c("osis")
        osis.get_pipeline(PipelineName=s.pipeline)
        steps.do(name, osis.delete_pipeline, PipelineName=s.pipeline)
    except (ClientError, BotoCoreError) as err:
        if getattr(err, "response", {}).get("Error", {}).get("Code") == "AccessDeniedException" and \
                "service control policy" in str(err):
            steps.unchecked.append(f"{name} (denied by a service control policy)")
        else:
            steps._note(name, err)

    lam = c("lambda")
    for fn in (s.function, f"{s.prefix}-reembed-enqueue"):
        # Mappings are looked up by function name even when the function is gone: deleting a function does not
        # delete its event source mappings (https://docs.aws.amazon.com/lambda/latest/api/API_DeleteFunction.html).
        with steps.part(f"event source mappings of {fn}"):
            for m in lam.list_event_source_mappings(FunctionName=fn)["EventSourceMappings"]:
                if m.get("State") == "Deleting":        # an earlier clean-up asked for it; a second delete is refused
                    steps.absent.append(f"event source mapping {m['UUID']} (already being deleted)")
                    continue
                steps.do(f"event source mapping {m['UUID']}", lam.delete_event_source_mapping, UUID=m["UUID"])
        with steps.part(f"Lambda function {fn}"):
            lam.get_function(FunctionName=fn)              # absent: ResourceNotFoundException ends this part
            steps.do(f"Lambda function {fn}", lam.delete_function, FunctionName=fn)
        # The log group stays after the function: Lambda never removes it.
        group = f"/aws/lambda/{fn}"
        with steps.part(f"log group {group}"):
            logs = c("logs")
            found = logs.describe_log_groups(logGroupNamePrefix=group)["logGroups"]
            if any(g["logGroupName"] == group for g in found):
                steps.do(f"log group {group}", logs.delete_log_group, logGroupName=group)
            else:
                steps.absent.append(f"log group {group}")

    with steps.part("CloudWatch alarms"):
        cw = c("cloudwatch")
        names = [s.alarm_iterator_age, s.alarm_failed_records]
        found = [a["AlarmName"] for a in cw.describe_alarms(AlarmNames=names)["MetricAlarms"]]
        for name in names:
            if name in found:
                steps.do(f"alarm {name}", cw.delete_alarms, AlarmNames=[name])
            else:
                steps.absent.append(f"alarm {name}")
    sqs = c("sqs")
    for q in (s.failure_queue, f"{s.prefix}-reembed", f"{s.prefix}-reembed-dlq"):
        with steps.part(f"queue {q}"):
            url = sqs.get_queue_url(QueueName=q)["QueueUrl"]
            steps.do(f"queue {q}", sqs.delete_queue, QueueUrl=url)

    aoss = aoss or c("opensearchserverless")  # tests pass a small fake: moto lacks some of these calls
    for name in (s.collection, s.vector_collection):
        with steps.part(f"collection {name}"):
            found = aoss.batch_get_collection(names=[name]).get("collectionDetails", [])
            if not found:
                steps.absent.append(f"collection {name}")
            elif found[0].get("status") == "DELETING":
                steps.absent.append(f"collection {name} (already being deleted)")
                if not dry_run:
                    _wait(lambda n=name: aoss.batch_get_collection(names=[n]).get("collectionDetails"),
                          f"collection {name}", wait_s)
            elif steps.do(f"collection {name} (its indexes go with it)", aoss.delete_collection,
                          id=found[0]["id"]) and not dry_run:
                _wait(lambda n=name: aoss.batch_get_collection(names=[n]).get("collectionDetails"),
                      f"collection {name}", wait_s)
        # Policies are looked up by name: a list call returns one page, and teammates share the account.
        with steps.part(f"data access policy {name}"):
            aoss.get_access_policy(name=name, type="data")
            steps.do(f"data access policy {name}", aoss.delete_access_policy, name=name, type="data")
        for kind in ("network", "encryption"):
            with steps.part(f"{kind} policy {name}"):
                aoss.get_security_policy(name=name, type=kind)
                steps.do(f"{kind} policy {name}", aoss.delete_security_policy, name=name, type=kind)
    for group in (s.group, s.vector_group):        # the vector group only exists after ext-embeddings
        with steps.part(f"collection group {group}"):
            groups = aoss.batch_get_collection_group(names=[group]).get("collectionGroupDetails", [])
            if groups:
                if steps.do(f"collection group {group}", aoss.delete_collection_group, id=groups[0]["id"]) \
                        and not dry_run:
                    # Wait until it is gone, so a run right after this clean-up does not find it half deleted.
                    _wait(lambda g=group: aoss.batch_get_collection_group(names=[g]).get("collectionGroupDetails"),
                          f"collection group {group}", wait_s)
            else:
                steps.absent.append(f"collection group {group}")
    # ext-zero-etl: the export and dead-letter objects, and the bucket when the recipe named it.
    with steps.part("S3 bucket of ext-zero-etl"):
        s3 = c("s3")
        bucket = s.bucket(c("sts").get_caller_identity()["Account"])
        s3.head_bucket(Bucket=bucket)                       # absent: 404 ends this part
        # Only this PREFIX's objects: teammates may share a bucket (BUCKET), each under their own PREFIX/.
        for prefix in (f"{s.prefix}/ddb-export/", f"{s.prefix}/osis-dlq/"):
            for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
                keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
                if not keys:
                    continue
                what = f"{len(keys)} objects under s3://{bucket}/{prefix}"
                if dry_run:
                    steps.removed.append(what)
                    continue
                # DeleteObjects answers 200 also when some keys were not deleted: those are in Errors.
                errors = s3.delete_objects(Bucket=bucket, Delete={"Objects": keys}).get("Errors", [])
                if errors:
                    steps.failed.append(f"{what}: {len(errors)} not deleted, first {errors[0].get('Key')}: "
                                        f"{errors[0].get('Code')}")
                else:
                    steps.removed.append(what)
        if not s.bucket_env:
            steps.do(f"bucket {bucket}", s3.delete_bucket, Bucket=bucket)

    with steps.part(f"DynamoDB table {s.table}"):
        ddb = c("dynamodb")
        if ddb.describe_table(TableName=s.table)["Table"]["TableStatus"] == "DELETING":
            steps.absent.append(f"DynamoDB table {s.table} (already being deleted)")
        else:
            steps.do(f"DynamoDB table {s.table} (its stream goes with it)", ddb.delete_table, TableName=s.table)

    from main.infra import ROLE_SUFFIXES, TAG
    iam = c("iam")
    for name in (f"{s.prefix}{suffix}" for suffix in ROLE_SUFFIXES):
        with steps.part(f"IAM role {name}"):
            if TAG not in iam.list_role_tags(RoleName=name)["Tags"]:
                continue                              # not created by this recipe
            for pol in iam.list_role_policies(RoleName=name)["PolicyNames"]:
                if not dry_run:
                    iam.delete_role_policy(RoleName=name, PolicyName=pol)
            steps.do(f"IAM role {name}", iam.delete_role, RoleName=name)
    return steps


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true", help="list what would be deleted (the default)")
    p.add_argument("--yes", action="store_true", help="delete; without it, only list what would be deleted")
    args = p.parse_args(argv)
    args.dry_run = args.dry_run or not args.yes
    s = Settings()
    steps = clean_up(s, args.dry_run)
    code = steps.report()
    for item in steps.unchecked:
        print(f"{'not checked':<8} {item}")
    verb = "would be removed" if args.dry_run else "removed"
    print(f"{len(steps.removed)} resources {verb}, {len(steps.failed)} failed, for PREFIX={s.prefix} in {s.region}")
    if code:
        print("Something is left: read the FAILED lines, fix the reason, and run clean_up.py again.")
    if args.dry_run:
        print("Nothing deleted. Run again with --yes to delete these.")
    sys.exit(code)


if __name__ == "__main__":
    main()

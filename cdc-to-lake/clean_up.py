"""Remove everything a real run of this recipe created in your AWS account, for this PREFIX.

  PREFIX=recipe-yourname uv run python clean_up.py            list only: nothing is deleted
  PREFIX=recipe-yourname uv run python clean_up.py --yes      delete

First the clean-ups of ext-maintenance (the nightly compaction function, its schedules, alarms and topic) and of
ext-scheduled-merge (the hourly merge), each in its folder's clean_up.py; then the rest of the recipe.

Removes, in this order: the nightly schedules (ext-maintenance), the alarms (ext-lag-alarm), the event source
mapping, the Lambda function and its log group, the Firehose stream, the Glue view, table and database, the lake's
objects in S3 (and the bucket, when the recipe named it), the DynamoDB table. The stream consumer goes first so
nothing writes while the rest is removed. Last, the IAM roles the recipe created (tagged recipe=cdc-to-lake); a role
passed in by ARN does not carry the tag and stays.

Every step is attempted, also after one fails. A step that was refused is printed as FAILED and the exit code is
then 1; what was already gone is printed as absent.
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


LAG_ALARMS = ("cdc-lag", "cdc-iterator-age")     # ext-lag-alarm/lag.py names its alarms {PREFIX}-<these>

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


def delete_mapping(lam, uuid: str, wait_s: float = 60.0, poll_s: float = 3.0) -> None:
    """Delete one event source mapping. Lambda answers "in use" while the mapping is changing state.

    A mapping that is already being deleted is done; one that is still being created or updated is tried again
    until wait_s is over.
    """
    end = time.time() + wait_s
    while True:
        try:
            lam.delete_event_source_mapping(UUID=uuid)
            return
        except lam.exceptions.ResourceInUseException:
            if lam.get_event_source_mapping(UUID=uuid)["State"] == "Deleting":
                return
            if time.time() >= end:
                raise
            time.sleep(poll_s)


def delete_keys(s3, bucket: str, keys: list[dict]) -> None:
    """DeleteObjects answers 200 and lists the keys it could not delete: that list is a failure, not a success."""
    errors = s3.delete_objects(Bucket=bucket, Delete={"Objects": keys, "Quiet": True}).get("Errors", [])
    if errors:
        first = errors[0]
        raise ClientError({"Error": {"Code": first.get("Code", "DeleteError"), "Message":
                                     f"{len(errors)} of {len(keys)} objects not deleted, first: {first.get('Key')}: "
                                     f"{first.get('Message', '')}"}}, "DeleteObjects")


def clean_up(s: Settings, dry_run: bool = False, session=None, mapping_wait_s: float = 60.0) -> Steps:
    """Attempt every step; the returned Steps says what was removed, what was absent and what failed."""
    session = session or boto3.Session(region_name=s.region)
    c = lambda svc: session.client(svc, region_name=s.region)  # noqa: E731
    steps = Steps(dry_run)

    sch = c("scheduler")
    for name in (f"{s.prefix}-lake-optimize", f"{s.prefix}-lake-vacuum"):
        with steps.part(f"schedule {name}"):
            sch.get_schedule(Name=name)
            steps.do(f"schedule {name}", sch.delete_schedule, Name=name)

    cw = c("cloudwatch")
    with steps.part("alarms"):
        # the exact names ext-lag-alarm/lag.py gives its alarms; any other alarm, yours or a teammate's, stays
        mine = [f"{s.prefix}-{name}" for name in LAG_ALARMS]
        alarms = [a["AlarmName"] for a in cw.describe_alarms(AlarmNames=mine)["MetricAlarms"]]
        if alarms:
            steps.do(f"alarms {', '.join(alarms)}", cw.delete_alarms, AlarmNames=alarms)

    lam = c("lambda")
    with steps.part(f"Lambda function {s.function}"):
        lam.get_function(FunctionName=s.function)
        for m in lam.list_event_source_mappings(FunctionName=s.function)["EventSourceMappings"]:
            if m.get("State") == "Deleting":              # an earlier clean-up asked for it: nothing left to do
                steps.absent.append(f"event source mapping {m['UUID']} (already being deleted)")
            else:
                steps.do(f"event source mapping {m['UUID']}", delete_mapping, lam=lam, uuid=m["UUID"],
                         wait_s=mapping_wait_s)
        steps.do(f"Lambda function {s.function}", lam.delete_function, FunctionName=s.function)
    # Lambda makes this log group on the function's first call and deleting the function leaves it. Looked up apart
    # from the function, so a group left by an earlier clean-up is removed too.
    logs, group = c("logs"), f"/aws/lambda/{s.function}"
    with steps.part(f"log group {group}"):
        if any(g["logGroupName"] == group
               for g in logs.describe_log_groups(logGroupNamePrefix=group)["logGroups"]):
            steps.do(f"log group {group}", logs.delete_log_group, logGroupName=group)
        else:
            steps.absent.append(f"log group {group}")

    sqs = c("sqs")
    with steps.part(f"queue {s.failures_queue}"):
        url = sqs.get_queue_url(QueueName=s.failures_queue)["QueueUrl"]      # a missing queue: absent
        steps.do(f"queue {s.failures_queue}", sqs.delete_queue, QueueUrl=url)

    fh = c("firehose")
    with steps.part(f"Firehose stream {s.delivery_stream}"):
        status = fh.describe_delivery_stream(DeliveryStreamName=s.delivery_stream)[
            "DeliveryStreamDescription"]["DeliveryStreamStatus"]
        if status == "DELETING":
            steps.absent.append(f"Firehose stream {s.delivery_stream} (already being deleted)")
        else:
            steps.do(f"Firehose stream {s.delivery_stream}", fh.delete_delivery_stream,
                     DeliveryStreamName=s.delivery_stream)

    glue = c("glue")
    with steps.part(f"Glue database {s.database}"):
        glue.get_database(Name=s.database)
        for page in glue.get_paginator("get_tables").paginate(DatabaseName=s.database):
            for t in page["TableList"]:
                steps.do(f"Glue table {s.database}.{t['Name']}", glue.delete_table, DatabaseName=s.database,
                         Name=t["Name"])
        steps.do(f"Glue database {s.database}", glue.delete_database, Name=s.database)

    s3 = c("s3")
    with steps.part("the lake's bucket"):
        bucket = s.bucket(c("sts").get_caller_identity()["Account"])
        with steps.part(f"bucket {bucket}"):
            s3.head_bucket(Bucket=bucket)                 # a missing bucket answers 404: absent
            emptied = True
            # Our prefixes only, in case BUCKET points at a bucket the team uses for other things.
            for prefix in ("lake/", "athena-results/", "firehose-errors/", "freshness/"):
                for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
                    keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
                    if keys:
                        emptied &= steps.do(f"{len(keys)} objects under s3://{bucket}/{prefix}", delete_keys, s3=s3,
                                            bucket=bucket, keys=keys)
            if not s.bucket_env and emptied:              # the recipe named and created this bucket
                steps.do(f"bucket {bucket}", s3.delete_bucket, Bucket=bucket)

    ddb = c("dynamodb")
    with steps.part(f"DynamoDB table {s.table}"):
        if ddb.describe_table(TableName=s.table)["Table"]["TableStatus"] == "DELETING":
            steps.absent.append(f"DynamoDB table {s.table} (already being deleted)")
        else:
            steps.do(f"DynamoDB table {s.table} (its stream goes with it)", ddb.delete_table, TableName=s.table)

    from main.infra import TAG, role_names
    iam = c("iam")
    for name in role_names(s).values():
        with steps.part(f"IAM role {name}"):
            if TAG not in iam.list_role_tags(RoleName=name)["Tags"]:
                continue                                  # not created by this recipe
            for pol in iam.list_role_policies(RoleName=name)["PolicyNames"]:
                if not dry_run:
                    iam.delete_role_policy(RoleName=name, PolicyName=pol)
            steps.do(f"IAM role {name}", iam.delete_role, RoleName=name)
    return steps


EXTENSION_CLEAN_UPS = ("ext-maintenance", "ext-scheduled-merge")   # each has its own clean_up.py


def extension_clean_ups(dry_run: bool, log=print) -> int:
    """Run the clean-up of each extension that deploys its own resources (the nightly compaction function and its
    alarms; the hourly merge), first, so no scheduled job runs while the rest goes. Lines about nothing (0 objects,
    0 items) are left out. Returns 1 when one of them failed."""
    import importlib.util
    from pathlib import Path

    code = 0
    for folder in EXTENSION_CLEAN_UPS:
        path = Path(__file__).resolve().parent / folder / "clean_up.py"
        spec = importlib.util.spec_from_file_location(f"{folder.replace('-', '_')}_clean_up", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        lines: list[str] = []
        code |= module.clean(yes=not dry_run, log=lines.append)
        for line in lines:
            if line.startswith(("absent", "kept")) or " 0 objects " in line or " 0 items " in line:
                continue
            log(line.replace("would remove ", "would remove ", 1) if dry_run else line)
    return code


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true", help="list what would be deleted (the default)")
    p.add_argument("--yes", action="store_true", help="delete; without it, only list what would be deleted")
    args = p.parse_args()
    args.dry_run = args.dry_run or not args.yes
    s = Settings()
    ext_code = extension_clean_ups(args.dry_run)
    steps = clean_up(s, args.dry_run)
    code = steps.report() | ext_code
    verb = "would be removed" if args.dry_run else "removed"
    print(f"{len(steps.removed)} resources {verb} for PREFIX={s.prefix} in {s.region}")
    if code:
        print(f"{len(steps.failed)} steps FAILED: the clean-up is NOT complete. Fix what they say, then run it again")
    if args.dry_run:
        print("Nothing deleted. Run again with --yes to delete these.")
    return code


if __name__ == "__main__":
    sys.exit(main())

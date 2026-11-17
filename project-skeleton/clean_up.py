"""Remove everything this recipe creates for one $PREFIX, in AWS (ca-central-1) or on this machine.

    PREFIX=recipe-yourname uv run python clean_up.py            # AWS: all three representatives, then .local/
    uv run python clean_up.py --local                        # only the offline files in .local/

Every step is attempted, also after one fails. Each line says what was removed, what was absent (already gone), or
what FAILED and why; with a failed step the exit code is 1. Running it twice is safe.
"""
import argparse
import shutil
import sys
from contextlib import contextmanager

from botocore.exceptions import BotoCoreError, ClientError

from skeleton.config import TAG, account_id, names, session
from skeleton.lake_local import LOCAL_ROOT

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


def empty_bucket(s3, bucket: str) -> None:
    """Delete every object version, then the bucket (exports, Parquet files and Athena results go with it)."""
    paginator = s3.get_paginator("list_object_versions")
    for page in paginator.paginate(Bucket=bucket):
        objs = [{"Key": o["Key"], "VersionId": o["VersionId"]}
                for o in page.get("Versions", []) + page.get("DeleteMarkers", [])]
        for i in range(0, len(objs), 1000):
            s3.delete_objects(Bucket=bucket, Delete={"Objects": objs[i:i + 1000], "Quiet": True})
    s3.delete_bucket(Bucket=bucket)             # refuses a bucket that still holds an object: reported as FAILED


def workgroup_exists(athena, name: str) -> bool:
    """Looked up in the list, page by page: asking for a missing workgroup by name is answered with the same
    error code as a bad request, which says too little to call it absent."""
    kw = {}
    while True:
        page = athena.list_work_groups(**kw)
        if name in [w["Name"] for w in page["WorkGroups"]]:
            return True
        if not page.get("NextToken"):
            return False
        kw = {"NextToken": page["NextToken"]}


def delete_role(iam, name: str) -> None:
    for p in iam.list_role_policies(RoleName=name)["PolicyNames"]:
        iam.delete_role_policy(RoleName=name, PolicyName=p)
    iam.delete_role(RoleName=name)


def clean_aws(sess, n: dict, log=print) -> Steps:
    fh, kin, lam, logs = (sess.client(s) for s in ("firehose", "kinesis", "lambda", "logs"))
    ddb, glue, athena, s3, iam = (sess.client(s) for s in ("dynamodb", "glue", "athena", "s3", "iam"))
    steps = Steps()

    def unless_deleting(what: str, status: str, fn, **kw) -> None:
        """A resource an earlier clean-up is still deleting needs no second delete. A Kinesis or Firehose stream would
        refuse it ("in use"); DynamoDB accepts it without an error, so the table is only reported as already going."""
        if status == "DELETING":
            steps.absent.append(f"{what} (already being deleted)")
        else:
            steps.do(what, fn, **kw)

    # Stop what bills by the hour first: the Firehose stream, then the Kinesis stream (shard-hours).
    with steps.part(f"Firehose stream {n['firehose']}"):
        status = fh.describe_delivery_stream(DeliveryStreamName=n["firehose"])[
            "DeliveryStreamDescription"]["DeliveryStreamStatus"]
        unless_deleting(f"Firehose stream {n['firehose']}", status, fh.delete_delivery_stream,
                        DeliveryStreamName=n["firehose"])
    with steps.part(f"Kinesis stream {n['stream']}"):
        status = kin.describe_stream_summary(StreamName=n["stream"])["StreamDescriptionSummary"]["StreamStatus"]
        unless_deleting(f"Kinesis stream {n['stream']}", status, kin.delete_stream, StreamName=n["stream"],
                        EnforceConsumerDeletion=True)
    steps.do(f"function URL of {n['function']}", lam.delete_function_url_config, FunctionName=n["function"])
    steps.do(f"Lambda function {n['function']}", lam.delete_function, FunctionName=n["function"])
    steps.do(f"log group {n['log_group']}", logs.delete_log_group, logGroupName=n["log_group"])
    with steps.part(f"DynamoDB table {n['table']}"):
        status = ddb.describe_table(TableName=n["table"])["Table"]["TableStatus"]
        # The table has point-in-time recovery, so DynamoDB keeps a system backup of it after the delete, for 35 days
        # at no cost (https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/PointInTimeRecovery_Howitworks.html).
        unless_deleting(f"DynamoDB table {n['table']} (DynamoDB keeps a system backup of it for 35 days, at no cost)", status,
                        ddb.delete_table, TableName=n["table"])
    steps.do(f"Glue database {n['database']} and its tables and views", glue.delete_database, Name=n["database"])
    with steps.part(f"Athena workgroup {n['workgroup']}"):
        if workgroup_exists(athena, n["workgroup"]):
            steps.do(f"Athena workgroup {n['workgroup']}", athena.delete_work_group, WorkGroup=n["workgroup"],
                     RecursiveDeleteOption=True)
        else:
            steps.absent.append(f"Athena workgroup {n['workgroup']}")
    steps.do(f"S3 bucket {n['bucket']} and every object in it", empty_bucket, s3=s3, bucket=n["bucket"])
    for role in (n["firehose_role"], n["function_role"]):
        with steps.part(f"IAM role {role}"):
            # Only a role this recipe created (it carries the recipe tag); a role you passed in by ARN stays.
            if TAG in iam.list_role_tags(RoleName=role)["Tags"]:
                steps.do(f"IAM role {role}", delete_role, iam=iam, name=role)
            else:
                steps.absent.append(f"IAM role {role} (kept: not created by this recipe)")
    steps.report(log)
    return steps


def clean_local(log=print) -> None:
    if LOCAL_ROOT.exists():
        shutil.rmtree(LOCAL_ROOT)
        log(f"removed  {LOCAL_ROOT}/")
    else:
        log(f"absent   {LOCAL_ROOT}/")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--local", action="store_true", help="only remove the offline files")
    args = p.parse_args()
    failed = []
    if not args.local:
        sess = session()
        failed = clean_aws(sess, names(account=account_id(sess))).failed
    clean_local()
    if failed:
        print(f"{len(failed)} steps FAILED: the clean-up is NOT complete. Fix what they say, then run it again")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

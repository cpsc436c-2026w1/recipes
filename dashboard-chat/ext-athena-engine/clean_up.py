"""Remove what ext-athena-engine created, for this PREFIX; keep what it borrowed.

  PREFIX=recipe-yourname uv run python ext-athena-engine/clean_up.py          # the plan: what it would delete
  PREFIX=recipe-yourname uv run python ext-athena-engine/clean_up.py --yes    # delete

Deletes: the function <PREFIX>-chat-query, its log group, the role <PREFIX>-chat-reader (its inline policy first),
the table <PREFIX>-chat-usage, the workgroup <PREFIX>-chat-wg when the ledger records it created (with its query
history), the tables trips and zones and the database <prefix>_chat by name (Athena DDL made them, so they carry no
tags), and the prefixes chat/ and athena-results/chat-wg/ in the lake bucket.
Keeps: the bucket (scripts/lake.py down deletes it), the shared workgroups <PREFIX>-wg and <PREFIX>-ctas-wg, and the
table <PREFIX>-chat-traces (dashboard-chat's clean_up.py --yes deletes it). A team that set CHAT_DATABASE to its own lake
keeps that database: only <prefix>_chat is dropped.
"""

from __future__ import annotations

import argparse
import sys
from contextlib import contextmanager
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError

HERE = Path(__file__).resolve().parent
# Run as a script, Python puts this folder first on sys.path; the parent (for main/) and scripts/ go last, so
# nothing here hides a module of the same name in the parent (its clean_up.py).
sys.path += [p for p in (str(HERE.parent / "scripts"),) if p not in sys.path]

from settings import REGION, Names  # noqa: E402

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



def planned(names: Names) -> list[str]:
    return [f"lambda function {names.function}", f"log group {names.log_group}", f"iam role {names.role}",
            f"dynamodb table {names.usage_table}",
            f"athena workgroup {names.workgroup} (only if the ledger records it created)",
            f"glue tables {names.glue_prefix}_chat.trips and .zones, database {names.glue_prefix}_chat",
            f"s3 prefixes {names.data_prefix} and {names.results_prefix} in <PREFIX>-lake-<account>"]


def _delete_prefix(s3, bucket: str, prefix: str) -> int:
    n = 0
    for page in s3.get_paginator("list_object_versions").paginate(Bucket=bucket, Prefix=prefix):
        objs = [{"Key": v["Key"], "VersionId": v["VersionId"]}
                for v in page.get("Versions", []) + page.get("DeleteMarkers", [])]
        for i in range(0, len(objs), 1000):
            s3.delete_objects(Bucket=bucket, Delete={"Objects": objs[i:i + 1000], "Quiet": True})
        n += len(objs)
    return n


def clean_up(names: Names, yes: bool = False, sess=None) -> Steps:
    from tags import record, recorded

    steps = Steps(dry_run=not yes)
    if not yes:
        steps.removed = planned(names)
        return steps
    sess = sess or boto3.Session(region_name=REGION)
    lam, logs, iam = sess.client("lambda"), sess.client("logs"), sess.client("iam")
    ddb, athena, glue, s3 = (sess.client(n) for n in ("dynamodb", "athena", "glue", "s3"))
    # The function first: it is what bills per call, and nothing can invoke it after.
    if steps.do(f"lambda function {names.function}", lam.delete_function, FunctionName=names.function):
        record("lambda-function", names.function, "deleted", script="ext-athena-engine/clean_up.py")
    if steps.do(f"log group {names.log_group}", logs.delete_log_group, logGroupName=names.log_group):
        record("log-group", names.log_group, "deleted", script="ext-athena-engine/clean_up.py")
    with steps.part(f"iam role {names.role}"):
        for p in iam.list_role_policies(RoleName=names.role)["PolicyNames"]:
            iam.delete_role_policy(RoleName=names.role, PolicyName=p)
        if steps.do(f"iam role {names.role}", iam.delete_role, RoleName=names.role):
            record("iam-role", names.role, "deleted", script="ext-athena-engine/clean_up.py")
    if steps.do(f"dynamodb table {names.usage_table}", ddb.delete_table, TableName=names.usage_table):
        record("dynamodb-table", names.usage_table, "deleted", script="ext-athena-engine/clean_up.py")
    what = f"athena workgroup {names.workgroup}"
    state = recorded("athena-workgroup", names.workgroup)
    if state == "created":
        if steps.do(what, athena.delete_work_group, WorkGroup=names.workgroup, RecursiveDeleteOption=True):
            record("athena-workgroup", names.workgroup, "deleted", script="ext-athena-engine/clean_up.py")
    else:
        steps.absent.append(f"{what} (ledger: {state or 'no line'}; kept)")
    db = f"{names.glue_prefix}_chat"
    for table in ("trips", "zones"):
        steps.do(f"glue table {db}.{table}", glue.delete_table, DatabaseName=db, Name=table)
    if steps.do(f"glue database {db}", glue.delete_database, Name=db):
        record("glue-database", db, "deleted", script="ext-athena-engine/clean_up.py")
    with steps.part("s3 prefixes"):
        import lake

        bucket = lake.bucket_name(sess)
        for prefix in (names.data_prefix, names.results_prefix):
            n = _delete_prefix(s3, bucket, prefix)
            (steps.removed if n else steps.absent).append(f"s3://{bucket}/{prefix} ({n} objects)")
        record("s3-prefix", f"{bucket}/{names.data_prefix}", "deleted", script="ext-athena-engine/clean_up.py")
    return steps


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--yes", action="store_true", help="delete; without it, print what would be deleted")
    args = p.parse_args(argv)
    names = Names()
    steps = clean_up(names, yes=args.yes)
    code = steps.report()
    if not args.yes:
        print(f"dry run for PREFIX={names.prefix} in {REGION}: nothing was contacted")
        print("Nothing deleted. Run again with --yes to delete these.")
    return code


if __name__ == "__main__":
    sys.exit(main())

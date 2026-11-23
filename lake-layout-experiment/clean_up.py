"""Remove what the lake layout experiment made, for this PREFIX; keep what it borrowed or shares.

  PREFIX=recipe-yourname uv run python clean_up.py          # lists what it would delete (read-only calls)
  PREFIX=recipe-yourname uv run python clean_up.py --plan   # the plan by name, with no AWS call
  PREFIX=recipe-yourname uv run python clean_up.py --yes    # delete (the same as experiment.py clean --yes)

Deletes: the tables and the database <prefix>_layout by name (Athena DDL makes them, so they carry no tags), the
prefix layout/ in the lake bucket, the function <PREFIX>-duckdb-query, its log group and its role.
Keeps: the bucket and athena-results/ (its 7-day rule deletes the results), the data source `course`, both shared
workgroups <PREFIX>-wg and <PREFIX>-ctas-wg (scripts/lake.py down deletes those the ledger records created, after
every recipe's clean-up), and results.duckdb and summary.md.
"""

from __future__ import annotations

import argparse
import sys
from contextlib import contextmanager
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError

HERE = Path(__file__).resolve().parent
sys.path += [p for p in (str(HERE / "scripts"),) if p not in sys.path]

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



def planned(names) -> list[str]:
    return [f"glue tables of {names.database} and the database", "s3 prefix layout/ in <PREFIX>-lake-<account>",
            f"lambda function {names.function}", f"log group {names.log_group}", f"iam role {names.role}"]


def found(names, sess) -> list[str]:
    """What a bare run with AWS access lists, read-only: each Glue table of the database by name and the number of
    object versions under layout/, so you can check that they are this experiment's before --yes."""
    glue, s3 = sess.client("glue"), sess.client("s3")
    out = []
    try:
        tables = [t["Name"] for page in glue.get_paginator("get_tables").paginate(DatabaseName=names.database)
                  for t in page["TableList"]]
        out += [f"glue table {names.database}.{t}" for t in tables] + [f"glue database {names.database}"]
    except ClientError as err:
        if err.response["Error"]["Code"] not in GONE:
            raise
    try:
        n = sum(len(page.get("Versions", [])) + len(page.get("DeleteMarkers", []))
                for page in s3.get_paginator("list_object_versions").paginate(Bucket=names.bucket, Prefix="layout/"))
    except ClientError as err:
        if err.response["Error"]["Code"] not in GONE:
            raise
        n = 0                                     # no bucket: lake.py down has run
    if n:
        out.append(f"s3://{names.bucket}/layout/ ({n} objects)")
    return out + [f"lambda function {names.function}", f"log group {names.log_group}", f"iam role {names.role}"]


def clean_up(names, yes: bool = False, sess=None) -> Steps:
    from tags import record

    steps = Steps(dry_run=not yes)
    if not yes:
        steps.removed = planned(names) if sess is None else found(names, sess)
        return steps
    sess = sess or boto3.Session(region_name="ca-central-1")
    glue, s3, lam, logs, iam = (sess.client(n) for n in ("glue", "s3", "lambda", "logs", "iam"))
    if steps.do(f"lambda function {names.function}", lam.delete_function, FunctionName=names.function):
        record("lambda-function", names.function, "deleted", script="clean_up.py")
    if steps.do(f"log group {names.log_group}", logs.delete_log_group, logGroupName=names.log_group):
        record("log-group", names.log_group, "deleted", script="clean_up.py")
    with steps.part(f"iam role {names.role}"):
        for p in iam.list_role_policies(RoleName=names.role)["PolicyNames"]:
            iam.delete_role_policy(RoleName=names.role, PolicyName=p)
        if steps.do(f"iam role {names.role}", iam.delete_role, RoleName=names.role):
            record("iam-role", names.role, "deleted", script="clean_up.py")
    with steps.part(f"glue database {names.database}"):
        tables = [t["Name"] for page in glue.get_paginator("get_tables").paginate(DatabaseName=names.database)
                  for t in page["TableList"]]
        for t in tables:
            steps.do(f"glue table {names.database}.{t}", glue.delete_table, DatabaseName=names.database, Name=t)
        if steps.do(f"glue database {names.database}", glue.delete_database, Name=names.database):
            record("glue-database", names.database, "deleted", script="clean_up.py")
    with steps.part("s3 prefix layout/"):
        n = 0
        for page in s3.get_paginator("list_object_versions").paginate(Bucket=names.bucket, Prefix="layout/"):
            objs = [{"Key": v["Key"], "VersionId": v["VersionId"]}
                    for v in page.get("Versions", []) + page.get("DeleteMarkers", [])]
            for i in range(0, len(objs), 1000):
                s3.delete_objects(Bucket=names.bucket, Delete={"Objects": objs[i:i + 1000], "Quiet": True})
            n += len(objs)
        (steps.removed if n else steps.absent).append(f"s3://{names.bucket}/layout/ ({n} objects)")
    return steps


def main(argv=None) -> int:
    from names import Names

    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--yes", action="store_true", help="delete; without it, list what would be deleted")
    p.add_argument("--plan", action="store_true", help="without --yes: print the plan by name, with no AWS call")
    args = p.parse_args(argv)
    sess = None if (args.plan and not args.yes) else boto3.Session(region_name="ca-central-1")
    steps = clean_up(Names(sess=sess), yes=args.yes, sess=sess)
    code = steps.report()
    if not args.yes:
        print("the plan by name, no AWS call" if sess is None else
              "listed with read-only calls: check that every table and object is this experiment's")
        print("Nothing deleted. Run again with --yes to delete these.")
    return code


if __name__ == "__main__":
    sys.exit(main())

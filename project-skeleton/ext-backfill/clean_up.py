"""Remove what ext-backfill created for one $PREFIX, and nothing else.

    uv run python ext-backfill/clean_up.py           # what it would delete; nothing is deleted (PREFIX exported)
    uv run python ext-backfill/clean_up.py --yes     # delete it, then ask each service again
    uv run python ext-backfill/clean_up.py --local --yes           # only the offline files in .local/ext-backfill/

It deletes the table <PREFIX>-trips-by-id (kept when the ledger records it borrowed: a table that existed
before the load), the Glue tables trips_history, history_raw and the view trips_all of
<prefix>_lake, the objects under history/trips/ and imports/trips-by-id/ of the lake bucket, and history/trips.source. It keeps the
bucket (scripts/lake.py down deletes it), the workgroups <PREFIX>-wg and <PREFIX>-ctas-wg (lake.py down), the live
table trips (scheduled-merge's), the data source course (the course's) and your own files under raw/history/. The
database <prefix>_lake goes only when backfill.py created it and no table is left in it. Every step is attempted;
a refused one is printed FAILED and the exit code is 1. Running it twice is safe.
"""
from __future__ import annotations

import argparse
import importlib.util
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
for folder in (HERE.parent / "scripts", HERE.parent):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))

from skeleton.config import names, prefix  # noqa: E402

_spec = importlib.util.spec_from_file_location("skeleton_clean_up", HERE.parent / "clean_up.py")
_parent = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_parent)
Steps = _parent.Steps                     # project-skeleton's: every step attempted, only "not found" is absent

OWNED_TABLES = ("trips_all", "trips_history", "history_raw")
OWNED_PREFIXES = ("history/trips/", "history/trips.source", "imports/trips-by-id/")
LOCAL = Path(".local") / "ext-backfill"


def delete_prefix(s3, bucket: str, prefix_: str) -> int:
    n = 0
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix_):
        keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
        if keys:
            s3.delete_objects(Bucket=bucket, Delete={"Objects": keys, "Quiet": True})
            n += len(keys)
    return n


def count_prefix(s3, bucket: str, prefix_: str) -> int:
    return sum(len(p.get("Contents", [])) for p in
               s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix_))


def clean_aws(sess, yes: bool, log=print) -> int:
    from tags import record, recorded

    p = prefix()
    account = sess.client("sts").get_caller_identity()["Account"]
    n = names(p, account)
    db, bucket, table = n["database"], n["bucket"], f"{p}-trips-by-id"
    glue, s3, ddb = sess.client("glue"), sess.client("s3"), sess.client("dynamodb")
    steps = Steps(dry_run=not yes)

    borrowed = recorded("dynamodb-table", table) == "borrowed"
    with steps.part(f"DynamoDB table {table}"):
        ddb.describe_table(TableName=table)
        if borrowed:                 # it existed before the load (a live table): batch writes only added items
            log(f"kept     DynamoDB table {table}: it was there before backfill.py; its backfill items stay "
                f"(each has the load's load_id)")
        elif steps.do(f"DynamoDB table {table}", ddb.delete_table, TableName=table) and yes:
            record("dynamodb-table", table, "deleted", script="ext-backfill/clean_up.py")
    for t in OWNED_TABLES:
        with steps.part(f"Glue table {db}.{t}"):
            glue.get_table(DatabaseName=db, Name=t)
            if steps.do(f"Glue table {db}.{t}", glue.delete_table, DatabaseName=db, Name=t) and yes \
                    and recorded("glue-table", f"{db}.{t}") == "created":
                record("glue-table", f"{db}.{t}", "deleted", script="ext-backfill/clean_up.py")
    for pre in OWNED_PREFIXES:
        with steps.part(f"s3://{bucket}/{pre}"):
            found = count_prefix(s3, bucket, pre)
            if found:
                steps.do(f"s3://{bucket}/{pre}: {found} objects", delete_prefix, s3=s3, bucket=bucket, prefix_=pre)
            else:
                steps.absent.append(f"s3://{bucket}/{pre}")
    with steps.part(f"Glue database {db}"):
        if recorded("glue-database", db) == "created":
            left = [t["Name"] for t in glue.get_tables(DatabaseName=db).get("TableList", [])
                    if t["Name"] not in OWNED_TABLES]
            if left:
                log(f"kept     Glue database {db}: it still holds {', '.join(left)}")
            elif steps.do(f"Glue database {db}", glue.delete_database, Name=db) and yes:
                record("glue-database", db, "deleted", script="ext-backfill/clean_up.py")
        else:
            log(f"kept     Glue database {db}: backfill.py did not create it")
    for kept in (f"S3 bucket {bucket} (scripts/lake.py down)", f"Athena workgroups {p}-wg and {p}-ctas-wg "
                 f"(scripts/lake.py down)", "Athena data source course (the course's)",
                 f"Glue table {db}.trips (scheduled-merge's)"):
        log(f"kept     {kept}")
    code = steps.report(log)
    if yes:
        code = max(code, check_gone(sess, db, bucket, None if borrowed else table, log))
    return code


def check_gone(sess, db: str, bucket: str, table: str, log=print) -> int:
    """Ask each service again; only its own "not found" counts as gone."""
    glue, s3, ddb = sess.client("glue"), sess.client("s3"), sess.client("dynamodb")
    still = []
    try:
        status = ddb.describe_table(TableName=table)["Table"]["TableStatus"] if table else "DELETING"
        if status != "DELETING":
            still.append(f"DynamoDB table {table} ({status})")
    except ddb.exceptions.ResourceNotFoundException:
        pass
    for t in OWNED_TABLES:
        try:
            glue.get_table(DatabaseName=db, Name=t)
            still.append(f"Glue table {db}.{t}")
        except glue.exceptions.EntityNotFoundException:
            pass
    for pre in OWNED_PREFIXES:
        try:
            if count_prefix(s3, bucket, pre):
                still.append(f"s3://{bucket}/{pre}")
        except s3.exceptions.NoSuchBucket:
            pass
    for s in still:
        log(f"FAILED: still there: {s}")
    if not still:
        log("checked  nothing of ext-backfill is left (a table being deleted counts as gone)")
    return 1 if still else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--yes", action="store_true", help="delete; without it, only list what would be deleted")
    ap.add_argument("--local", action="store_true", help="only the offline files; no call to AWS")
    args = ap.parse_args(argv)
    if not args.local:
        import boto3
        code = clean_aws(boto3.session.Session(region_name="ca-central-1"), args.yes)
    else:
        code = 0
    if LOCAL.exists():
        if args.yes:
            shutil.rmtree(LOCAL)
        print(f"{'removed' if args.yes else 'would remove':<8} {LOCAL}/")
    else:
        print(f"absent   {LOCAL}/")
    if not args.yes:
        print("Nothing deleted. Run again with --yes to delete these.")
    return code


if __name__ == "__main__":
    sys.exit(main())

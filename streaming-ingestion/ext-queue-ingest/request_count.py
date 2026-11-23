"""Count the SQS requests a run made, by queue and action, from CloudTrail data events.

    uv run python ext-queue-ingest/request_count.py up                      # before the run: a trail and its bucket
    uv run python ext-queue-ingest/request_count.py count --start T --end T # after: requests per queue and action
    uv run python ext-queue-ingest/request_count.py down                    # what it would remove; nothing is deleted
    uv run python ext-queue-ingest/request_count.py down --yes              # the trail and its bucket removed

SQS bills every request (a ReceiveMessage that returns 1 message costs what one that returns 10 costs), but its
CloudWatch metrics count messages, and empty receives only: NumberOfMessagesReceived is messages, not calls. So
the calls Lambda's poller makes to gather a batch are not in CloudWatch. A trail of data events records each call.

`up` creates the bucket `$PREFIX-sqs-requests-<account>` (CloudTrail may write to it, only for this trail) and the
trail `$PREFIX-sqs-requests`, which logs the data events of queues whose name starts with $PREFIX only, and writes
both to the ledger. CloudTrail bills 0.10 USD per 100,000 data events; a rush-hour run is about 10,000. CloudTrail
delivers a call's event to the bucket within about 5 minutes (CloudTrail docs), so count 15 minutes after the run.
`clean_up.py` removes both as well.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for folder in (ROOT / "main", ROOT / "scripts"):
    sys.path.insert(0, str(folder))

import infra  # noqa: E402
from tags import record, tags  # noqa: E402


def names(prefix: str, account: str) -> dict:
    return {"trail": f"{prefix}-sqs-requests", "bucket": f"{prefix}-sqs-requests-{account}"}


def bucket_policy(bucket: str, trail_arn: str) -> dict:
    """CloudTrail may check the bucket's ACL and write under AWSLogs/, only for this trail (aws:SourceArn)."""
    cond = {"StringEquals": {"aws:SourceArn": trail_arn}}
    return {"Version": "2012-10-17", "Statement": [
        {"Sid": "TrailAcl", "Effect": "Allow", "Principal": {"Service": "cloudtrail.amazonaws.com"},
         "Action": "s3:GetBucketAcl", "Resource": f"arn:aws:s3:::{bucket}", "Condition": cond},
        {"Sid": "TrailWrite", "Effect": "Allow", "Principal": {"Service": "cloudtrail.amazonaws.com"},
         "Action": "s3:PutObject", "Resource": f"arn:aws:s3:::{bucket}/AWSLogs/*",
         "Condition": {"StringEquals": {**cond["StringEquals"], "s3:x-amz-acl": "bucket-owner-full-control"}}}]}


def selectors(region: str, account: str, prefix: str) -> list[dict]:
    """Data events of the queues named $PREFIX… only: other queues of the account are not logged or billed."""
    return [{"Name": f"SQS data events of {prefix} queues", "FieldSelectors": [
        {"Field": "eventCategory", "Equals": ["Data"]},
        {"Field": "resources.type", "Equals": ["AWS::SQS::Queue"]},
        {"Field": "resources.ARN", "StartsWith": [f"arn:aws:sqs:{region}:{account}:{prefix}"]}]}]


def up(session, prefix: str) -> dict:
    region, acct = session.region_name, infra.account_id(session)
    n = names(prefix, acct)
    s3, ct = session.client("s3"), session.client("cloudtrail")
    script, clean = "ext-queue-ingest/request_count.py", "clean_up.py"
    try:
        s3.create_bucket(Bucket=n["bucket"], CreateBucketConfiguration={"LocationConstraint": region})
    except s3.exceptions.BucketAlreadyOwnedByYou:
        pass
    s3.put_bucket_tagging(Bucket=n["bucket"], Tagging={"TagSet": tags("s3")})
    record("s3-bucket", n["bucket"], arn=f"arn:aws:s3:::{n['bucket']}", script=script, clean=clean)
    trail_arn = f"arn:aws:cloudtrail:{region}:{acct}:trail/{n['trail']}"
    s3.put_bucket_policy(Bucket=n["bucket"], Policy=json.dumps(bucket_policy(n["bucket"], trail_arn)))
    try:
        ct.create_trail(Name=n["trail"], S3BucketName=n["bucket"], IsMultiRegionTrail=False,
                        IncludeGlobalServiceEvents=False, TagsList=tags("cloudtrail"))
    except ct.exceptions.TrailAlreadyExistsException:
        pass
    record("cloudtrail-trail", n["trail"], arn=trail_arn, script=script, clean=clean)
    ct.put_event_selectors(TrailName=n["trail"], AdvancedEventSelectors=selectors(region, acct, prefix))
    ct.start_logging(Name=n["trail"])
    return n


def count_events(events: list[dict], start: datetime | None = None, end: datetime | None = None) -> Counter:
    """(queue name, action) -> calls, for the events between start and end."""
    got: Counter = Counter()
    for e in events:
        t = datetime.fromisoformat(e["eventTime"].replace("Z", "+00:00"))
        if (start and t < start) or (end and t > end):
            continue
        queue = ""
        for r in e.get("resources") or []:
            if r.get("type") == "AWS::SQS::Queue":
                queue = r.get("ARN", "").rsplit(":", 1)[-1]
        if not queue:
            url = (e.get("requestParameters") or {}).get("queueUrl", "")
            queue = url.rsplit("/", 1)[-1]
        got[(queue, e["eventName"])] += 1
    return got


def read_events(s3, bucket: str) -> list[dict]:
    events = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix="AWSLogs/"):
        for o in page.get("Contents", []):
            if o["Key"].endswith(".json.gz"):
                body = gzip.decompress(s3.get_object(Bucket=bucket, Key=o["Key"])["Body"].read())
                events += json.loads(body).get("Records", [])
    return events


def count(session, prefix: str, start: datetime | None, end: datetime | None) -> dict:
    n = names(prefix, infra.account_id(session))
    got = count_events(read_events(session.client("s3"), n["bucket"]), start, end)
    return {f"{q} {a}": c for (q, a), c in sorted(got.items())}


def down(session, prefix: str, dry_run: bool = False) -> list[str]:
    n = names(prefix, infra.account_id(session))
    s3, ct = session.client("s3"), session.client("cloudtrail")
    done = []
    try:
        ct.get_trail(Name=n["trail"])
        if not dry_run:
            ct.delete_trail(Name=n["trail"])
        done.append(f"trail {n['trail']}")
    except ct.exceptions.TrailNotFoundException:
        pass
    try:
        keys = [o["Key"] for page in s3.get_paginator("list_objects_v2").paginate(Bucket=n["bucket"])
                for o in page.get("Contents", [])]
        for i in range(0, len(keys), 1000):
            if not dry_run:
                s3.delete_objects(Bucket=n["bucket"], Delete={"Objects": [{"Key": k} for k in keys[i:i + 1000]],
                                                               "Quiet": True})
        if not dry_run:
            s3.delete_bucket(Bucket=n["bucket"])
        done.append(f"bucket {n['bucket']} ({len(keys)} objects)")
    except s3.exceptions.NoSuchBucket:
        pass
    return done


def main(argv=None) -> int:
    import boto3

    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("action", choices=["up", "count", "down"])
    p.add_argument("--start", help="ISO 8601, UTC: count calls from this time")
    p.add_argument("--end", help="ISO 8601, UTC: count calls up to this time")
    p.add_argument("--dry-run", action="store_true", help="down: list only (the default)")
    p.add_argument("--yes", action="store_true", help="down: delete; without it, only list what would be deleted")
    p.add_argument("--json", help="count: also write the counts to this file")
    args = p.parse_args(argv)
    prefix = os.environ.get("PREFIX", "recipe")
    session = boto3.Session(region_name=os.environ.get("AWS_REGION", "ca-central-1"))
    if args.action == "up":
        n = up(session, prefix)
        print(f"trail {n['trail']} logs the SQS calls of queues {prefix}* to s3://{n['bucket']}/")
        return 0
    if args.action == "down":
        dry = args.dry_run or not args.yes
        verb = "would remove" if dry else "removed"
        for line in down(session, prefix, dry) or ["nothing: trail and bucket absent"]:
            print(f"{verb} {line}")
        if dry:
            print("Nothing deleted. Run again with --yes to delete these.")
        return 0
    parse = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc) if s else None  # noqa: E731
    got = count(session, prefix, parse(args.start), parse(args.end))
    for k, v in got.items():
        print(f"{v:>7} {k}")
    if args.json:
        Path(args.json).write_text(json.dumps(got, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())

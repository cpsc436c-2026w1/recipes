"""Remove everything this recipe creates (main scenario and the four extensions), by exact name.

    PREFIX=recipe-ana uv run python clean_up.py               # list what would be removed, remove nothing
    PREFIX=recipe-ana uv run python clean_up.py --yes         # remove it

Only names built from PREFIX are touched (main/infra.py `names`), so a teammate's resources with another prefix stay.
The poller's schedule group goes first (it would start the function again), then each function's mappings. In the
shared lake bucket only the poller's `feed/` prefix is deleted: the bucket and `raw/trip-events/` belong to the
lake. The run log `$PREFIX-runs` is deleted only when this folder's ledger (state/ledger.jsonl) says it created it.
`--dry-run` looks each resource up and prints `would remove` or `absent` for it, and deletes nothing. `--yes` (or
no flag) deletes; `--yes` is accepted so the course's teardown can call every recipe's clean-up the same way.
Safe to run twice: a resource that is already gone is reported as absent. Every step is attempted, also after one
fails; a failed step is printed as FAILED and the exit code is then 1.
"""

import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError

sys.path.insert(0, str(Path(__file__).parent / "main"))
sys.path.insert(0, str(Path(__file__).parent / "scripts"))
import infra  # noqa: E402

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
            if fn and (not self.dry_run):
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


def do(steps: Steps, what: str, fn=None, probe=None, **kw) -> bool:
    """steps.do, and with a dry run first `probe`: a look-up that raises a "not found" error when the resource is
    gone, so the dry run prints `absent` for it instead of `would remove`. (Steps is a shared helper, kept as its
    copies are: tools/shared_helpers.toml.)"""
    if steps.dry_run and probe:
        try:
            probe()
        except (ClientError, BotoCoreError) as err:
            steps._note(what, err)
            return False
    return steps.do(what, fn, **kw)


def _log_group_probe(logs, name: str):
    def probe():
        found = logs.describe_log_groups(logGroupNamePrefix=name)["logGroups"]
        if not any(g["logGroupName"] == name for g in found):
            raise ClientError({"Error": {"Code": "ResourceNotFoundException", "Message": name}}, "DescribeLogGroups")
    return probe


def clean(session, prefix: str, mapping_wait_s: float = 60.0, dry_run: bool = False) -> Steps:
    """Attempt every delete, print one line for each, and return the steps (`failed` is empty when all went well).
    With dry_run, look everything up and delete nothing."""
    n = infra.names(prefix)
    lam, iam, logs = session.client("lambda"), session.client("iam"), session.client("logs")
    sqs, sns = session.client("sqs"), session.client("sns")
    ddb, kinesis = session.client("dynamodb"), session.client("kinesis")
    steps = Steps(dry_run)

    # ext-source-poller: the schedule group first; deleting a group deletes its schedules
    scheduler = session.client("scheduler")
    with steps.part(f"schedule group {n['schedule_group']}"):
        group = scheduler.get_schedule_group(Name=n["schedule_group"])   # a missing group: ResourceNotFoundException
        if group.get("State") == "DELETING":          # `run.py stop` asked for it: Scheduler deletes a group slowly
            steps.absent.append(f"schedule group {n['schedule_group']} (already being deleted)")
        else:
            steps.do(f"schedule group {n['schedule_group']} (and its schedule {n['schedule']})",
                     scheduler.delete_schedule_group, Name=n["schedule_group"])

    roles = {fn: f"{fn}-role" for fn in [n["consumer"], n["windows"], n["dashboard"], n["history"], n["poller"]]}
    roles[n["trip_consumer"]] = n["trip_consumer_role"]
    for fn, role in roles.items():
        with steps.part(f"mappings of {fn}"):
            for m in lam.list_event_source_mappings(FunctionName=fn)["EventSourceMappings"]:
                what = f"mapping {m['UUID']} of {fn}"
                if m.get("State") == "Deleting":            # an earlier clean-up asked for it: nothing left to do
                    steps.absent.append(f"{what} (already being deleted)")
                else:
                    steps.do(what, delete_mapping, lam=lam, uuid=m["UUID"], wait_s=mapping_wait_s)
        do(steps, f"function {fn}", lam.delete_function, probe=lambda fn=fn: lam.get_function(FunctionName=fn),
                 FunctionName=fn)
        do(steps, f"log group /aws/lambda/{fn}", logs.delete_log_group,
           probe=_log_group_probe(logs, f"/aws/lambda/{fn}"), logGroupName=f"/aws/lambda/{fn}")
        _role(steps, iam, role)
    _role(steps, iam, n["scheduler_role"])

    with steps.part(f"topic {n['topic']}"):
        topic_arn = f"arn:aws:sns:{session.region_name}:{infra.account_id(session)}:{n['topic']}"
        # delete_topic succeeds for a topic that is not there, so ask first: a missing topic raises NotFound here
        sns.get_topic_attributes(TopicArn=topic_arn)
        for sub in sns.list_subscriptions_by_topic(TopicArn=topic_arn)["Subscriptions"]:
            if sub["SubscriptionArn"].startswith("arn:"):
                steps.do(f"subscription {sub['Endpoint']}", sns.unsubscribe, SubscriptionArn=sub["SubscriptionArn"])
        steps.do(f"topic {n['topic']}", sns.delete_topic, TopicArn=topic_arn)

    for q in [n["consumer_failures"], n["windows_failures"], n["dashboard"], f"{n['dashboard']}-dlq",
              n["history"], f"{n['history']}-dlq", n["trip_queue"], n["trip_dlq"]]:
        with steps.part(f"queue {q}"):
            url = sqs.get_queue_url(QueueName=q)["QueueUrl"]
            steps.do(f"queue {q}", sqs.delete_queue, QueueUrl=url)

    # a stream that an earlier clean-up is still deleting refuses a second delete ("in use"); DynamoDB accepts a second
    # delete of a table in DELETING without an error. Look first, so either is reported as already going
    for t in [n["table"], n["windows_table"], n["dashboard"], n["history"], n["trips_table"], n["slots_table"]]:
        with steps.part(f"table {t}"):
            if ddb.describe_table(TableName=t)["Table"]["TableStatus"] == "DELETING":
                steps.absent.append(f"table {t} (already being deleted)")
            else:
                steps.do(f"table {t}", ddb.delete_table, TableName=t)

    with steps.part(f"stream {n['stream']}"):
        summary = kinesis.describe_stream_summary(StreamName=n["stream"])["StreamDescriptionSummary"]
        if summary["StreamStatus"] == "DELETING":
            steps.absent.append(f"stream {n['stream']} (already being deleted)")
        else:
            steps.do(f"stream {n['stream']}", kinesis.delete_stream, StreamName=n["stream"],
                     EnforceConsumerDeletion=True)

    bucket = f"{prefix}-lake-{infra.account_id(session)}"
    with steps.part(f"prefix feed/ of bucket {bucket}"):
        s3 = session.client("s3")
        keys = [o["Key"] for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix="feed/")
                for o in page.get("Contents", [])]
        for i in range(0, len(keys), 1000):
            steps.do(f"{len(keys[i:i + 1000])} objects under s3://{bucket}/feed/", s3.delete_objects, Bucket=bucket,
                     Delete={"Objects": [{"Key": k} for k in keys[i:i + 1000]], "Quiet": True})
        if not keys:
            steps.absent.append(f"objects under s3://{bucket}/feed/")

    # ext-queue-ingest/request_count.py: the trail that counted the SQS calls, and its bucket
    with steps.part(f"trail {prefix}-sqs-requests"):
        sys.path.insert(0, str(Path(__file__).parent / "ext-queue-ingest"))
        import request_count

        for what in request_count.down(session, prefix, dry_run=True):
            steps.do(what, lambda: request_count.down(session, prefix) if what.startswith("trail") else None)
        if not any(x.startswith(("trail", "bucket")) and f"{prefix}-sqs-requests" in x for x in steps.removed):
            steps.absent.append(f"trail and bucket {prefix}-sqs-requests")

    run_log = f"{prefix}-runs"
    with steps.part(f"table {run_log}"):
        from tags import record, recorded

        if recorded("dynamodb-table", run_log) == "created":
            if do(steps, f"table {run_log} (the ledger says this folder created it)", ddb.delete_table,
                  TableName=run_log, probe=lambda: ddb.describe_table(TableName=run_log)) \
                    and not steps.dry_run:
                record("dynamodb-table", run_log, "deleted", script="clean_up.py")

    if steps.report():
        print("the clean-up is NOT complete: fix what FAILED says, then run clean_up.py again")
    return steps


def _role(steps: Steps, iam, role: str) -> None:
    do(steps, f"policy of role {role}", iam.delete_role_policy,
       probe=lambda: iam.get_role_policy(RoleName=role, PolicyName="least-privilege"),
       RoleName=role, PolicyName="least-privilege")
    do(steps, f"role {role}", iam.delete_role, probe=lambda: iam.get_role(RoleName=role), RoleName=role)


def main(argv: list[str] | None = None) -> int:
    import argparse
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true", help="list what would be deleted (the default)")
    p.add_argument("--yes", action="store_true", help="delete; without it, only list what would be deleted")
    args = p.parse_args(argv)
    dry = args.dry_run or not args.yes
    prefix = os.environ.get("PREFIX", "recipe")
    region = os.environ.get("AWS_REGION", "ca-central-1")
    print(f"{'would remove' if dry else 'removing'} the streaming-ingestion resources with prefix {prefix!r} in {region}")
    steps = clean(boto3.Session(region_name=region), prefix, dry_run=dry)
    if dry:
        print("Nothing deleted. Run again with --yes to delete these.")
    return 1 if steps.failed else 0


if __name__ == "__main__":
    sys.exit(main())

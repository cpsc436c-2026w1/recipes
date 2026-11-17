"""Remove everything this recipe creates (main scenario and both extensions), by exact name.

    PREFIX=recipe-ana uv run python clean_up.py

Only names built from PREFIX are touched (main/infra.py `names`), so a teammate's resources with another prefix stay.
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


def clean(session, prefix: str, mapping_wait_s: float = 60.0) -> Steps:
    """Attempt every delete, print one line for each, and return the steps (`failed` is empty when all went well)."""
    n = infra.names(prefix)
    lam, iam, logs = session.client("lambda"), session.client("iam"), session.client("logs")
    sqs, sns = session.client("sqs"), session.client("sns")
    ddb, kinesis = session.client("dynamodb"), session.client("kinesis")
    steps = Steps()

    for fn in [n["consumer"], n["windows"], n["dashboard"], n["history"]]:
        with steps.part(f"mappings of {fn}"):
            for m in lam.list_event_source_mappings(FunctionName=fn)["EventSourceMappings"]:
                what = f"mapping {m['UUID']} of {fn}"
                if m.get("State") == "Deleting":            # an earlier clean-up asked for it: nothing left to do
                    steps.absent.append(f"{what} (already being deleted)")
                else:
                    steps.do(what, delete_mapping, lam=lam, uuid=m["UUID"], wait_s=mapping_wait_s)
        steps.do(f"function {fn}", lam.delete_function, FunctionName=fn)
        steps.do(f"log group /aws/lambda/{fn}", logs.delete_log_group, logGroupName=f"/aws/lambda/{fn}")
        role = f"{fn}-role"
        steps.do(f"policy of role {role}", iam.delete_role_policy, RoleName=role, PolicyName="least-privilege")
        steps.do(f"role {role}", iam.delete_role, RoleName=role)

    with steps.part(f"topic {n['topic']}"):
        topic_arn = f"arn:aws:sns:{session.region_name}:{infra.account_id(session)}:{n['topic']}"
        # delete_topic succeeds for a topic that is not there, so ask first: a missing topic raises NotFound here
        sns.get_topic_attributes(TopicArn=topic_arn)
        for sub in sns.list_subscriptions_by_topic(TopicArn=topic_arn)["Subscriptions"]:
            if sub["SubscriptionArn"].startswith("arn:"):
                steps.do(f"subscription {sub['Endpoint']}", sns.unsubscribe, SubscriptionArn=sub["SubscriptionArn"])
        steps.do(f"topic {n['topic']}", sns.delete_topic, TopicArn=topic_arn)

    for q in [n["consumer_failures"], n["windows_failures"], n["dashboard"], f"{n['dashboard']}-dlq",
              n["history"], f"{n['history']}-dlq"]:
        with steps.part(f"queue {q}"):
            url = sqs.get_queue_url(QueueName=q)["QueueUrl"]
            steps.do(f"queue {q}", sqs.delete_queue, QueueUrl=url)

    # a stream that an earlier clean-up is still deleting refuses a second delete ("in use"); DynamoDB accepts a second
    # delete of a table in DELETING without an error. Look first, so either is reported as already going
    for t in [n["table"], n["windows_table"], n["dashboard"], n["history"]]:
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

    if steps.report():
        print("the clean-up is NOT complete: fix what FAILED says, then run clean_up.py again")
    return steps


if __name__ == "__main__":
    prefix = os.environ.get("PREFIX", "recipe")
    region = os.environ.get("AWS_REGION", "ca-central-1")
    print(f"removing the streaming-ingestion resources with prefix {prefix!r} in {region}")
    sys.exit(1 if clean(boto3.Session(region_name=region), prefix).failed else 0)

"""Remove everything a real run of this recipe created in your AWS account, for this PREFIX.

  PREFIX=recipe-yourname uv run python clean_up.py --dry-run  print the names it would delete; no AWS call
  PREFIX=recipe-yourname uv run python clean_up.py            delete

Removes: DynamoDB tables {PREFIX}-chat-traces, -chat-cache, -chat-spans; CloudWatch alarms {PREFIX}-chat-errors,
-chat-slow-answers, -chat-daily-cost (only these: a team's own alarm whose name starts the same way stays);
SNS topic {PREFIX}-chat-alarms and its subscriptions. A CloudWatch metric cannot be deleted (it expires 15 months after
its last data point), so stop sending metrics (stop the app) when you are done. Local files in output/ and data/ stay; delete them by hand if you want.
Every step is tried, also after one fails; a failure is printed and the exit code is 1.
"""

from __future__ import annotations

import argparse
import sys
from contextlib import contextmanager

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from main.config import Settings

# The alarm names ext-trace-alarms/alarms.py creates: {PREFIX}-chat-<slug>, one per THRESHOLDS key.
ALARM_SLUGS = ("errors", "slow-answers", "daily-cost")

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


def planned(s: Settings) -> list[str]:
    """The names this recipe creates for this PREFIX, built from the settings alone: no AWS call."""
    names = [f"dynamodb table {t}" for t in (s.traces_table, s.cache_table, s.spans_table)]
    names += [f"cloudwatch alarm {s.alarm_name(slug)}" for slug in ALARM_SLUGS]
    names.append(f"sns topic {s.topic_name}")
    return names


def clean_up(s: Settings, dry_run: bool = False, ddb=None, cw=None, sns=None) -> Steps:
    steps = Steps(dry_run)
    if dry_run:
        steps.removed = planned(s)
        return steps
    ddb = ddb or boto3.client("dynamodb", region_name=s.region)
    cw = cw or boto3.client("cloudwatch", region_name=s.region)
    sns = sns or boto3.client("sns", region_name=s.region)
    for table in (s.traces_table, s.cache_table, s.spans_table):
        what = f"dynamodb table {table}"
        with steps.part(what):
            status = ddb.describe_table(TableName=table)["Table"]["TableStatus"]
            if status == "DELETING":
                # An earlier clean-up already asked for it. The API reference says a second DeleteTable on a
                # table in the DELETING state returns no error; the call is left out, so the table is reported as
                # absent, not as removed a second time.
                # https://docs.aws.amazon.com/amazondynamodb/latest/APIReference/API_DeleteTable.html (read 2026-11-13)
                steps.absent.append(f"{what} (already being deleted)")
            else:
                steps.do(what, ddb.delete_table, TableName=table)
    with steps.part("cloudwatch alarms"):
        # Only the recipe's own alarms, by name: a team's alarm that shares the prefix is not this recipe's to delete.
        wanted = [s.alarm_name(slug) for slug in ALARM_SLUGS]
        found = {a["AlarmName"] for a in cw.describe_alarms(AlarmNames=wanted)["MetricAlarms"]}
        for name in wanted:
            if name in found:
                steps.do(f"cloudwatch alarm {name}", cw.delete_alarms, AlarmNames=[name])
            else:
                steps.absent.append(f"cloudwatch alarm {name}")
    what = f"sns topic {s.topic_name}"
    with steps.part(what):
        arns = [t["TopicArn"] for page in sns.get_paginator("list_topics").paginate() for t in page["Topics"]
                if t["TopicArn"].split(":")[-1] == s.topic_name]
        for arn in arns:
            steps.do(what, sns.delete_topic, TopicArn=arn)  # deletes its subscriptions too
        if not arns:
            steps.absent.append(what)
    return steps


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    s = Settings()
    steps = clean_up(s, args.dry_run)
    if args.dry_run:
        for r in steps.removed:
            print(f"would remove, if it exists: {r}")
        print(f"{len(steps.removed)} names for PREFIX={s.prefix} in {s.region}; nothing was contacted or deleted")
        return
    for r in steps.removed:
        print(f"removed {r}")
    for r in steps.absent:
        print(f"absent {r}")
    print(f"{len(steps.removed)} resources removed for PREFIX={s.prefix} in {s.region}")
    if steps.failed:
        for line in steps.failed:
            print(f"FAILED {line}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

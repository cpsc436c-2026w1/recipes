"""Remove everything a real run of this recipe created, for this PREFIX.

  PREFIX=recipe-<you> uv run python clean_up.py --dry-run   list only
  PREFIX=recipe-<you> uv run python clean_up.py             delete

Removes: CloudWatch alarms {PREFIX}-app-*, dashboard {PREFIX}-app, Lambda function {PREFIX}-app (its reserved
concurrency goes with it), log group /aws/lambda/{PREFIX}-app (the EMF lines), AWS Budgets budget {PREFIX}-monthly
(the function and the budget only when they carry the tag recipe=limits-and-alarms; one without it is refused),
SNS topic {PREFIX}-limits-alarms and its subscriptions, and IAM role {PREFIX}-app-role when this recipe created it
(a role named in LAMBDA_ROLE_ARN is left alone). CloudWatch does not offer a call that deletes metric data;
the metrics stop when the function is gone and expire on their own.

Every step is tried, also after one fails (a sandbox that refuses AWS Budgets still gets its function and alarms
removed); a failure is printed and the exit code is 1.
"""

from __future__ import annotations

import argparse
import sys
from contextlib import contextmanager
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError

sys.path.insert(0, str(Path(__file__).resolve().parent / "main"))

from config import ALARMS, Settings  # noqa: E402
from limits import RECIPE_TAG, budget_is_ours, function_is_ours, not_ours  # noqa: E402

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


def clean_up(s: Settings, c: dict, dry_run: bool = False) -> Steps:
    steps = Steps(dry_run)
    cw, lam, logs, budgets, sns, iam = c["cw"], c["lam"], c["logs"], c["budgets"], c["sns"], c["iam"]

    with steps.part("cloudwatch alarms"):
        wanted = [s.alarm_name(x) for x in ALARMS]
        found = [a["AlarmName"] for a in cw.describe_alarms(AlarmNames=wanted)["MetricAlarms"]]
        for name in wanted:
            if name in found:
                steps.do(f"cloudwatch alarm {name}", cw.delete_alarms, AlarmNames=[name])
            else:
                steps.absent.append(f"cloudwatch alarm {name}")

    what = f"cloudwatch dashboard {s.dashboard_name}"
    with steps.part(what):
        if any(d["DashboardName"] == s.dashboard_name
               for d in cw.list_dashboards(DashboardNamePrefix=s.dashboard_name)["DashboardEntries"]):
            steps.do(what, cw.delete_dashboards, DashboardNames=[s.dashboard_name])
        else:
            steps.absent.append(what)

    # Only the function this recipe created (its name and its tag); one with the name and without the tag stays.
    what = f"lambda function {s.function_name}"
    function = "unknown"  # "ours", "not ours", "gone", or "unknown" when its tag could not be read
    with steps.part(what):
        try:
            ours = function_is_ours(lam, s.function_name)
        except ClientError as err:
            if err.response.get("Error", {}).get("Code") in GONE:
                function = "gone"
            raise
        function = "ours" if ours else "not ours"
        if ours:
            steps.do(what, lam.delete_function, FunctionName=s.function_name)
        else:
            steps.failed.append(f"{what}: refused: {not_ours('lambda function', s.function_name)}")

    # Lambda made this log group on the function's first run; deleting the function leaves it, and its stored
    # log lines are billed until it is deleted. The log group of a function that is not ours stays with it.
    group = f"/aws/lambda/{s.function_name}"
    what = f"log group {group}"
    with steps.part(what):
        if function == "not ours":
            steps.failed.append(f"{what}: refused: it belongs to the function {s.function_name}, kept above")
        elif function == "unknown":
            steps.failed.append(f"{what}: refused: the tag of the function {s.function_name} could not be read, "
                                f"so its log group is kept")
        elif any(g["logGroupName"] == group for g in logs.describe_log_groups(logGroupNamePrefix=group)["logGroups"]):
            steps.do(what, logs.delete_log_group, logGroupName=group)
        else:
            steps.absent.append(what)

    what = f"budget {s.budget_name}"
    with steps.part(what):
        account = c["sts"].get_caller_identity()["Account"]
        budgets.describe_budget(AccountId=account, BudgetName=s.budget_name)
        if budget_is_ours(budgets, account, s.budget_name):
            steps.do(what, budgets.delete_budget, AccountId=account, BudgetName=s.budget_name)
        else:
            steps.failed.append(f"{what}: refused: {not_ours('budget', s.budget_name)}")

    what = f"sns topic {s.topic_name}"
    with steps.part(what):
        arns = [t["TopicArn"] for page in sns.get_paginator("list_topics").paginate() for t in page["Topics"]
                if t["TopicArn"].split(":")[-1] == s.topic_name]
        for arn in arns:
            # Only the topic this recipe created (its name and its tag); a team's topic with the name stays.
            if RECIPE_TAG in sns.list_tags_for_resource(ResourceArn=arn)["Tags"]:
                steps.do(what, sns.delete_topic, TopicArn=arn)  # its subscriptions go with it
            else:
                steps.failed.append(f"{what}: refused: {not_ours('sns topic', s.topic_name)}")
        if not arns:
            steps.absent.append(what)

    # Only the role this recipe created (its name and its tag); a role named in LAMBDA_ROLE_ARN stays.
    def delete_role() -> None:
        for name in iam.list_role_policies(RoleName=s.role_name)["PolicyNames"]:
            iam.delete_role_policy(RoleName=s.role_name, PolicyName=name)
        iam.delete_role(RoleName=s.role_name)

    what = f"iam role {s.role_name}"
    with steps.part(what):
        if RECIPE_TAG in iam.list_role_tags(RoleName=s.role_name)["Tags"]:
            steps.do(what, delete_role)
        else:
            steps.failed.append(f"{what}: refused: {not_ours('iam role', s.role_name)}")
    return steps


def clients(s: Settings) -> dict:
    """The clients of main/run.py and one more: `logs`, for the log group that Lambda creates, not the run."""
    return {"cw": boto3.client("cloudwatch", region_name=s.region), "lam": boto3.client("lambda", region_name=s.region),
            "logs": boto3.client("logs", region_name=s.region), "sns": boto3.client("sns", region_name=s.region),
            "budgets": boto3.client("budgets", region_name="us-east-1"),
            "iam": boto3.client("iam", region_name=s.region),
            "sts": boto3.client("sts", region_name=s.region)}


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)
    s = Settings()
    steps = clean_up(s, clients(s), args.dry_run)
    verb = "would remove" if args.dry_run else "removed"
    for what in steps.removed:
        print(f"{verb} {what}")
    for what in steps.absent:
        print(f"absent {what}")
    print(f"{len(steps.removed)} resources {'to remove' if args.dry_run else 'removed'} for PREFIX={s.prefix} in {s.region}")
    if steps.failed:
        for line in steps.failed:
            print(f"FAILED {line}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

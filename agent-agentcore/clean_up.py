"""Remove everything a deploy of this recipe created, for this PREFIX.

    uv run --env-file .env python clean_up.py     list only
    uv run --env-file .env python clean_up.py --yes               delete

Removes, in this order: the Runtime and its log groups, the Gateway target, the Gateway, the tools Lambda function
and its log group, the two Parameter Store parameters, the code zip and its bucket, the two DynamoDB tables, and
the roles the recipe created. A role you supplied (LAMBDA_ROLE_ARN, GATEWAY_ROLE_ARN, RUNTIME_ROLE_ARN) is left as
it is. Every step is tried, also after one fails; a failure is printed and the exit code is 1.
"""

from __future__ import annotations

import argparse
import sys
import time
from contextlib import contextmanager

from botocore.exceptions import BotoCoreError, ClientError

from main.config import Settings, load_settings
from main.infra import CODE_KEY, WAIT_S, WAIT_TRIES, Clients, account_id, find_gateway, find_runtime, find_target

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


def _gone(look, wait) -> bool:
    """True once look() raises "not found"; False when the resource is still there after WAIT_TRIES looks."""
    for _ in range(WAIT_TRIES):
        try:
            look()
        except ClientError as err:
            if err.response.get("Error", {}).get("Code", "") in GONE:
                return True
            raise
        wait(WAIT_S)
    return False


def clean_up(s: Settings, c: Clients, dry_run: bool = False, wait=time.sleep) -> Steps:
    steps = Steps(dry_run)
    ac = c.agentcore

    with steps.part("agentcore runtime"):
        runtime = find_runtime(s, c)
        what = f"agentcore runtime {s.runtime_name}"
        if not runtime:
            steps.absent.append(what)
        elif steps.do(what, ac.delete_agent_runtime, agentRuntimeId=runtime["id"]) and not dry_run:
            if not _gone(lambda: ac.get_agent_runtime(agentRuntimeId=runtime["id"]), wait):
                steps.failed.append(f"{what}: still there after {WAIT_TRIES * WAIT_S} s")

    with steps.part("agentcore gateway"):
        gateway_id = find_gateway(s, c)
        what = f"agentcore gateway {s.gateway_name}"
        if not gateway_id:
            steps.absent.append(what)
        else:
            # A Gateway with a target cannot be deleted: the target goes first, and is waited for.
            target_id = find_target(c, gateway_id)
            target = f"agentcore gateway target of {s.gateway_name}"
            if not target_id:
                steps.absent.append(target)
            elif steps.do(target, ac.delete_gateway_target, gatewayIdentifier=gateway_id, targetId=target_id) \
                    and not dry_run:
                if not _gone(lambda: ac.get_gateway_target(gatewayIdentifier=gateway_id, targetId=target_id), wait):
                    steps.failed.append(f"{target}: still there after {WAIT_TRIES * WAIT_S} s")
            steps.do(what, ac.delete_gateway, gatewayIdentifier=gateway_id)

    steps.do(f"lambda function {s.tools_function}", c.lambda_.delete_function, FunctionName=s.tools_function)

    with steps.part("log groups"):
        prefixes = (f"/aws/lambda/{s.tools_function}", f"/aws/bedrock-agentcore/runtimes/{s.runtime_name}-")
        groups = [g["logGroupName"] for p in prefixes
                  for g in c.logs.describe_log_groups(logGroupNamePrefix=p)["logGroups"]
                  if p.endswith("-") or g["logGroupName"] == p]
        for name in groups:
            steps.do(f"log group {name}", c.logs.delete_log_group, logGroupName=name)
        if not groups:
            steps.absent.append("log groups of the function and the runtime")

    with steps.part("parameters"):
        names = [s.token_param, s.logfire_param]
        # looked up first: Parameter Store's "not found" code is not one the shared GONE set reads as absent
        found = {p["Name"] for p in c.ssm.describe_parameters(
            ParameterFilters=[{"Key": "Name", "Option": "Equals", "Values": names}])["Parameters"]}
        for name in names:
            if name in found:
                steps.do(f"parameter {name}", c.ssm.delete_parameter, Name=name)
            else:
                steps.absent.append(f"parameter {name}")

    with steps.part("code bucket"):
        bucket = s.code_bucket(account_id(c))
        steps.do(f"s3 object s3://{bucket}/{CODE_KEY}", c.s3.delete_object, Bucket=bucket, Key=CODE_KEY)
        steps.do(f"s3 bucket {bucket}", c.s3.delete_bucket, Bucket=bucket)

    for name in (s.books_table, s.holds_table):
        steps.do(f"dynamodb table {name}", c.dynamodb.delete_table, TableName=name)

    for part in ("lambda", "gateway", "runtime"):
        name = s.role_name(part)
        if getattr(s, f"{part}_role_arn"):
            continue                                  # your role: the recipe did not create it and leaves it alone
        with steps.part(f"role {name}"):
            for policy in c.iam.list_role_policies(RoleName=name)["PolicyNames"]:
                steps.do(f"role policy {name}/{policy}", c.iam.delete_role_policy, RoleName=name, PolicyName=policy)
            steps.do(f"role {name}", c.iam.delete_role, RoleName=name)
    return steps


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true", help="list what would be deleted (the default)")
    p.add_argument("--yes", action="store_true", help="delete; without it, only list what would be deleted")
    args = p.parse_args(argv)
    args.dry_run = args.dry_run or not args.yes
    s = load_settings()
    steps = clean_up(s, Clients.for_region(s.aws_region), args.dry_run)
    code = steps.report()
    print(f"clean-up for PREFIX={s.prefix} in {s.aws_region}: {len(steps.removed)} "
          f"{'to remove' if args.dry_run else 'removed'}, {len(steps.absent)} absent, {len(steps.failed)} failed")
    if args.dry_run:
        print("Nothing deleted. Run again with --yes to delete these.")
    sys.exit(code)


if __name__ == "__main__":
    main()

"""Remove what this recipe and its extensions created for one $PREFIX in ca-central-1, then prove it is gone.

    PREFIX=recipe-yourname uv run python clean_up.py           # what it would remove; nothing is deleted
    PREFIX=recipe-yourname uv run python clean_up.py --yes     # remove it
    PREFIX=recipe-yourname uv run python clean_up.py --check   # list by tag, by schedule group, by name: all empty?
    PREFIX=recipe-yourname uv run python clean_up.py --ext dashboard-page [--yes]   # one extension only: main/'s API
                                                                                   # and the other extension stay

Order: the page's public URL first (nobody can call it while the rest goes), then the schedule group of
ext-dashboard-page (which deletes its schedules), its two functions, their log groups, the roles and the tables the
ledger state/ledger.jsonl records `created` (a table or role that was there before is kept); then what an
ext-read-path run left behind if it was stopped (the busy rider's items, the variable READ_PATH_ROUND); then the
main scenario's API with `bash clean_up.sh` (it lists what it would delete), then `bash clean_up.sh --yes` (URL, function, log group, role). Nothing here bills by the hour.
The tables of the dynamodb recipe stay: `bash ../dynamodb/clean_up.sh` (it lists what it would delete), then `bash ../dynamodb/clean_up.sh --yes` removes them. The shared workgroup
`<PREFIX>-wg` is deleted only by `scripts/lake.py down`.

Every step is attempted, also after one fails; with a failed step the exit code is 1. Running it twice is safe.
"""
import argparse
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT / "scripts")]
from tags import ledger_rows, prefix, record, recorded, tag_values  # noqa: E402

REGION = "ca-central-1"
EXTENSIONS = ("dashboard-page", "read-path")

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


def names() -> dict:
    p = prefix()
    return {"page": f"{p}-page", "job": f"{p}-dash-refresh", "group": f"{p}-dashboard-page",
            "roles": [f"{p}-page-role", f"{p}-dash-refresh-role", f"{p}-dash-scheduler-role", f"{p}-dash-url-role"],
            "tables": [f"{p}-zone-counts", f"{p}-runs"], "api": f"{p}-api", "rider_trips": f"{p}-rider-trips"}


def _last_state(type: str) -> dict:
    seen = {}
    for row in ledger_rows():
        if row["type"] == type:
            seen[row["name"]] = row["state"]
    return seen


def created(type: str) -> list[str]:
    """Names of this type whose last ledger line is `created`."""
    return [name for name, state in _last_state(type).items() if state == "created"]


def _found(fn, **kw) -> bool:
    """Ask the service for one resource by name; only its own 'not found' is False."""
    try:
        fn(**kw)
        return True
    except ClientError as err:
        if err.response["Error"]["Code"] in GONE:
            return False
        raise


def clean(sess, yes: bool, log=print, main_api: bool = True, only: str | None = None) -> Steps:
    """Every part, or with `only` one extension's (EXTENSIONS); main/'s API only when `only` is None."""
    n, steps = names(), Steps(dry_run=not yes)
    sched, lam, logs, iam, ddb = (sess.client(x) for x in ("scheduler", "lambda", "logs", "iam", "dynamodb"))

    def gone(type, name):
        if yes:
            record(type, name, "deleted", script="clean_up.py")

    if only in (None, "dashboard-page"):
        # ext-dashboard-page
        with steps.part(f"function URL of {n['page']}"):
            if _found(lam.get_function_url_config, FunctionName=n["page"]):
                steps.do(f"function URL of {n['page']}", lam.delete_function_url_config,
                         FunctionName=n["page"]) and gone("lambda-url", n["page"])
            else:
                steps.absent.append(f"function URL of {n['page']}")
        with steps.part(f"schedule group {n['group']}"):
            if _found(sched.get_schedule_group, Name=n["group"]):
                steps.do(f"schedule group {n['group']} and its schedules", sched.delete_schedule_group,
                         Name=n["group"]) and gone("schedule-group", n["group"])
            else:
                steps.absent.append(f"schedule group {n['group']}")
        for fn in (n["page"], n["job"]):
            with steps.part(f"Lambda function {fn}"):
                if _found(lam.get_function, FunctionName=fn):
                    steps.do(f"Lambda function {fn}", lam.delete_function, FunctionName=fn) and gone("lambda-function", fn)
                else:
                    steps.absent.append(f"Lambda function {fn}")
            group = f"/aws/lambda/{fn}"
            with steps.part(f"log group {group}"):
                if any(g["logGroupName"] == group for g in logs.describe_log_groups(logGroupNamePrefix=group)["logGroups"]):
                    steps.do(f"log group {group}", logs.delete_log_group, logGroupName=group) and gone("log-group", group)
                else:
                    steps.absent.append(f"log group {group}")
        for role in n["roles"]:
            with steps.part(f"IAM role {role}"):
                if not _found(iam.get_role, RoleName=role):
                    steps.absent.append(f"IAM role {role}")
                elif recorded("iam-role", role) != "created":
                    steps.absent.append(f"IAM role {role} (kept: this ledger did not create it)")
                else:
                    def delete_role(name=role):
                        for policy in iam.list_role_policies(RoleName=name)["PolicyNames"]:
                            iam.delete_role_policy(RoleName=name, PolicyName=policy)
                        iam.delete_role(RoleName=name)
                    steps.do(f"IAM role {role}", delete_role) and gone("iam-role", role)
        for table in n["tables"]:
            with steps.part(f"DynamoDB table {table}"):
                if not _found(ddb.describe_table, TableName=table):
                    steps.absent.append(f"DynamoDB table {table}")
                elif recorded("dynamodb-table", table) != "created":
                    steps.absent.append(f"DynamoDB table {table} (kept: borrowed, not created by this ledger)")
                else:
                    steps.do(f"DynamoDB table {table}", ddb.delete_table, TableName=table) and gone(
                        "dynamodb-table", table)

    if only in (None, "read-path"):
        # ext-read-path: only what a stopped run left (a finished run undoes both itself)
        for name in created("dynamodb-items"):
            table, _, rider = name.partition(":")
            with steps.part(f"items of {rider} in {table}"):
                keys = [it for page in ddb.get_paginator("query").paginate(
                    TableName=table, KeyConditionExpression="rider_id = :r", ProjectionExpression="rider_id, requested_at",
                    ExpressionAttributeValues={":r": {"S": rider}}) for it in page["Items"]]

                def delete_items(keys=keys, table=table):
                    for i in range(0, len(keys), 25):
                        batch = [{"DeleteRequest": {"Key": k}} for k in keys[i:i + 25]]
                        left = {table: batch}
                        while left:
                            left = ddb.batch_write_item(RequestItems=left).get("UnprocessedItems") or {}
                if keys:
                    steps.do(f"{len(keys)} items of {rider} in {table}", delete_items) and gone("dynamodb-items", name)
                else:
                    steps.absent.append(f"items of {rider} in {table}")
                    gone("dynamodb-items", name)
        for name in created("lambda-env"):
            fn, _, var = name.partition(":")
            with steps.part(f"variable {var} of {fn}"):
                env = lam.get_function_configuration(FunctionName=fn).get("Environment", {}).get("Variables", {})
                if var in env:
                    rest = {k: v for k, v in env.items() if k != var}
                    steps.do(f"variable {var} of {fn}", lam.update_function_configuration, FunctionName=fn,
                             Environment={"Variables": rest}) and gone("lambda-env", name)
                else:
                    steps.absent.append(f"variable {var} of {fn}")
                    gone("lambda-env", name)

    # main scenario: its own clean-up script (it keeps no ledger)
    if main_api and only is None:
        cmd = ["bash", str(ROOT / "clean_up.sh"), "--yes"]
        if not yes:
            steps.removed.append(f"the API {n['api']} (URL, function, log group, role) with: bash clean_up.sh --yes")
        else:
            p = subprocess.run(cmd, capture_output=True, text=True)
            log(p.stdout.rstrip())
            if p.returncode:
                log(p.stderr.rstrip())
                steps.failed.append(f"bash clean_up.sh --yes ended with exit code {p.returncode}")
            else:
                steps.removed.append(f"the API {n['api']} (bash clean_up.sh: see its lines above)")
    steps.report(log)
    return steps


def check(sess, log=print) -> int:
    """List what is left: by the Team tag, by schedule group, by name. 0 when nothing of this recipe is left."""
    n, team, left = names(), tag_values()["Team"], []
    try:
        for page in sess.client("resourcegroupstaggingapi").get_paginator("get_resources").paginate(
                TagFilters=[{"Key": "Team", "Values": [team]}]):
            for r in page["ResourceTagMappingList"]:
                # GetResources may list a resource for a while after it is deleted: its service is asked below
                log(f"tagged   {r['ResourceARN']}")
    except ClientError as err:
        log(f"REFUSED  tag:GetResources ({err.response['Error']['Code']}): only the name listing below")
    sched = sess.client("scheduler")
    left += [f"schedule group {g['Name']}" for page in sched.get_paginator("list_schedule_groups").paginate(
        NamePrefix=n["group"]) for g in page["ScheduleGroups"] if g["Name"] == n["group"]]
    lam = sess.client("lambda")
    mine = {n["page"], n["job"], n["api"]}
    left += [f"Lambda function {f['FunctionName']}" for page in lam.get_paginator("list_functions").paginate()
             for f in page["Functions"] if f["FunctionName"] in mine]
    groups = {f"/aws/lambda/{fn}" for fn in mine}
    left += [f"log group {g['logGroupName']}" for page in sess.client("logs").get_paginator(
        "describe_log_groups").paginate(logGroupNamePrefix=f"/aws/lambda/{prefix()}-") for g in page["logGroups"]
             if g["logGroupName"] in groups]
    roles = set(n["roles"]) | {f"{prefix()}-api-role"}
    left += [f"IAM role {r['RoleName']}" for page in sess.client("iam").get_paginator("list_roles").paginate()
             for r in page["Roles"] if r["RoleName"] in roles]
    tables = {t for t in n["tables"] if recorded("dynamodb-table", t) in ("created", "deleted")}
    left += [f"DynamoDB table {t}" for page in sess.client("dynamodb").get_paginator("list_tables").paginate()
             for t in page["TableNames"] if t in tables]
    for item in left:
        log(f"LEFT     {item}")
    log(f"check: {len(left)} left of this recipe's names for {prefix()}" + (" (empty)" if not left else ""))
    return 1 if left else 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--yes", action="store_true", help="delete; without it, list what would be removed")
    p.add_argument("--check", action="store_true", help="only list what is left")
    p.add_argument("--ext", choices=EXTENSIONS, help="only this extension's resources; main/'s API and the other "
                   "extension's are kept")
    args = p.parse_args(argv)
    sess = boto3.session.Session(region_name=REGION)
    if args.check:
        return check(sess)
    steps = clean(sess, args.yes, only=args.ext)
    if steps.failed:
        print(f"{len(steps.failed)} steps FAILED: the clean-up is NOT complete. Fix what they say, then run it again")
    elif not args.yes:
        print("Nothing deleted. Run again with --yes to delete these.")
    return 1 if steps.failed else 0


if __name__ == "__main__":
    sys.exit(main())

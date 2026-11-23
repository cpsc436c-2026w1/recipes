"""Remove everything this recipe created for one $PREFIX in ca-central-1, then prove it is gone.

    uv run python clean_up.py           # what it would remove; nothing is deleted
    uv run python clean_up.py --yes     # remove it
    uv run python clean_up.py --check   # list by tag, by schedule group, by name: all empty?
    uv run python scripts/lake.py down --yes   # last, after every recipe using the bucket has cleaned up

with PREFIX exported first, the same value the run used.

It deletes what this recipe owns, the hourly-billed part first: a Kinesis stream the ledger records `created` (the
live replay makes one), then the check's schedule group (which deletes its three schedules), the alarm, the
function, its log group, the topic (with its subscriptions), the roles and the run log the ledger records
`created`, and this recipe's own files under `raw/trip-events/` of the lake bucket: the slot files of the run ids
its replays used (the ledger's `s3-replay` rows and the folders of `evidence/`) and, when this ledger deployed the
check, the probe's files (run id `probe-<date>`). Other recipes replay into `raw/trip-events/` too; their files are
kept. A role or table that was there before is kept.
Every step is attempted, also after one fails; with a failed step the exit code is 1. Running it twice is safe.
"""
import argparse
import re
import sys
from contextlib import contextmanager
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "sim")]
from tags import ledger_rows, prefix, record, recorded, tag_values  # noqa: E402

REGION = "ca-central-1"
EVIDENCE = ROOT / "evidence"
SLOT_KEY = re.compile(r"^raw/trip-events/dt=\d{4}-\d{2}-\d{2}/\d{8}T\d{4}Z-(?P<run_id>.+)\.jsonl$")
PROBE_RUN = re.compile(r"^probe-\d{4}-\d{2}-\d{2}$")

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
    return {"function": f"{p}-live-check", "roles": [f"{p}-check-role", f"{p}-check-scheduler-role"],
            "topic": f"{p}-check-alerts", "group": f"{p}-simulated-runs", "alarm": f"{p}-check-failed",
            "log_group": f"/aws/lambda/{p}-live-check", "tables": [f"{p}-runs", f"{p}-sim-events"]}


def created(type: str) -> list[str]:
    """Names of this type whose last ledger line is `created`."""
    seen = {}
    for row in ledger_rows():
        if row["type"] == type:
            seen[row["name"]] = row["state"]
    return [name for name, state in seen.items() if state == "created"]


def own_run_ids() -> set[str]:
    """The run ids this recipe's replays used: the ledger's `s3-replay` rows and the folders of evidence/."""
    ids = {row["name"] for row in ledger_rows() if row["type"] == "s3-replay"}
    if EVIDENCE.is_dir():
        ids |= {d.name for d in EVIDENCE.iterdir() if d.is_dir()}
    return ids


def own_key(key: str, run_ids: set[str], probes: bool) -> bool:
    """A slot file of raw/trip-events/ that this recipe wrote: its run id is one of ours, or a probe's when this
    ledger deployed the check. Any other file under the prefix is another recipe's."""
    m = SLOT_KEY.match(key)
    if not m:
        return False
    return m["run_id"] in run_ids or (probes and bool(PROBE_RUN.match(m["run_id"])))


def _found(fn, **kw) -> bool:
    """Ask the service for one resource by name; only its own 'not found' is False."""
    try:
        fn(**kw)
        return True
    except ClientError as err:
        if err.response["Error"]["Code"] in GONE:
            return False
        raise


def clean(sess, yes: bool, log=print) -> Steps:
    n, steps = names(), Steps(dry_run=not yes)
    kin, sched, cw, lam, logs, sns, iam, ddb, s3, sts = (sess.client(x) for x in (
        "kinesis", "scheduler", "cloudwatch", "lambda", "logs", "sns", "iam", "dynamodb", "s3", "sts"))
    account = sts.get_caller_identity()["Account"]

    def gone(type, name):
        if yes:
            record(type, name, "deleted", script="clean_up.py")

    for stream in created("kinesis-stream"):                     # hourly: first
        with steps.part(f"Kinesis stream {stream}"):
            if _found(kin.describe_stream_summary, StreamName=stream):
                steps.do(f"Kinesis stream {stream}", kin.delete_stream, StreamName=stream,
                         EnforceConsumerDeletion=True) and gone("kinesis-stream", stream)
            else:
                steps.absent.append(f"Kinesis stream {stream}")
                gone("kinesis-stream", stream)
    with steps.part(f"schedule group {n['group']}"):
        if _found(sched.get_schedule_group, Name=n["group"]):
            steps.do(f"schedule group {n['group']} and its schedules", sched.delete_schedule_group,
                     Name=n["group"]) and gone("schedule-group", n["group"])
        else:
            steps.absent.append(f"schedule group {n['group']}")
    for name in created("schedule"):                           # only when the tagged group was refused
        with steps.part(f"schedule {name} (group default)"):
            if _found(sched.get_schedule, Name=name, GroupName="default"):
                steps.do(f"schedule {name} (group default)", sched.delete_schedule, Name=name,
                         GroupName="default") and gone("schedule", name)
            else:
                steps.absent.append(f"schedule {name} (group default)")
                gone("schedule", name)
    with steps.part(f"alarm {n['alarm']}"):
        if cw.describe_alarms(AlarmNames=[n["alarm"]])["MetricAlarms"]:
            steps.do(f"alarm {n['alarm']}", cw.delete_alarms, AlarmNames=[n["alarm"]]) and gone(
                "cloudwatch-alarm", n["alarm"])
        else:
            steps.absent.append(f"alarm {n['alarm']}")
    with steps.part(f"Lambda function {n['function']}"):
        if _found(lam.get_function, FunctionName=n["function"]):
            steps.do(f"Lambda function {n['function']}", lam.delete_function, FunctionName=n["function"]) and gone(
                "lambda-function", n["function"])
        else:
            steps.absent.append(f"Lambda function {n['function']}")
    with steps.part(f"log group {n['log_group']}"):
        if logs.describe_log_groups(logGroupNamePrefix=n["log_group"])["logGroups"]:
            steps.do(f"log group {n['log_group']}", logs.delete_log_group, logGroupName=n["log_group"]) and gone(
                "log-group", n["log_group"])
        else:
            steps.absent.append(f"log group {n['log_group']}")
    topic = f"arn:aws:sns:{REGION}:{account}:{n['topic']}"
    with steps.part(f"SNS topic {n['topic']}"):
        if _found(sns.get_topic_attributes, TopicArn=topic):
            steps.do(f"SNS topic {n['topic']} and its subscriptions", sns.delete_topic, TopicArn=topic) and gone(
                "sns-topic", n["topic"])
        else:
            steps.absent.append(f"SNS topic {n['topic']}")
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
                steps.absent.append(f"DynamoDB table {table} (kept: borrowed, another recipe's run log)")
            else:
                steps.do(f"DynamoDB table {table}", ddb.delete_table, TableName=table) and gone(
                    "dynamodb-table", table)
    bucket = f"{prefix()}-lake-{account}"
    run_ids = own_run_ids()
    probes = any(row["type"] == "lambda-function" and row["name"] == n["function"] for row in ledger_rows())
    with steps.part(f"s3://{bucket}/raw/trip-events/"):
        keys = [{"Key": o["Key"]} for page in s3.get_paginator("list_objects_v2").paginate(
            Bucket=bucket, Prefix="raw/trip-events/") for o in page.get("Contents", [])
            if own_key(o["Key"], run_ids, probes)]
        if not keys:
            steps.absent.append(f"s3://{bucket}/raw/trip-events/ (no files of this recipe's runs)")
        else:
            def delete_keys():
                for i in range(0, len(keys), 1000):
                    s3.delete_objects(Bucket=bucket, Delete={"Objects": keys[i:i + 1000], "Quiet": True})
            steps.do(f"{len(keys)} files of this recipe's runs under s3://{bucket}/raw/trip-events/", delete_keys)
    steps.report(log)
    return steps


def check(sess, log=print) -> int:
    """List what is left: by the Team tag, by schedule group, by name prefix. 0 when all three are empty."""
    p, team, left = prefix(), tag_values()["Team"], []
    tagging = sess.client("resourcegroupstaggingapi")
    try:
        for page in tagging.get_paginator("get_resources").paginate(TagFilters=[{"Key": "Team", "Values": [team]}]):
            for r in page["ResourceTagMappingList"]:
                # GetResources may list a resource for a while after it is deleted: ask its service (below)
                log(f"tagged   {r['ResourceARN']}")
    except ClientError as err:
        log(f"REFUSED  tag:GetResources ({err.response['Error']['Code']}): only the name-prefix listing below")
    sched = sess.client("scheduler")
    left += [f"schedule group {g['Name']}" for page in sched.get_paginator("list_schedule_groups").paginate(
        NamePrefix=p) for g in page["ScheduleGroups"]]
    left += [f"schedule {x['Name']} (group default)" for page in sched.get_paginator("list_schedules").paginate(
        GroupName="default", NamePrefix=f"{p}-") for x in page["Schedules"]]
    lam = sess.client("lambda")
    left += [f"Lambda function {f['FunctionName']}" for page in lam.get_paginator("list_functions").paginate()
             for f in page["Functions"] if f["FunctionName"].startswith(f"{p}-")]
    left += [f"log group {g['logGroupName']}" for page in sess.client("logs").get_paginator(
        "describe_log_groups").paginate(logGroupNamePrefix=f"/aws/lambda/{p}-") for g in page["logGroups"]]

    def pages(client, op, key, **kw):
        return [x for page in sess.client(client).get_paginator(op).paginate(**kw) for x in page[key]]

    left += [f"Kinesis stream {s}" for s in pages("kinesis", "list_streams", "StreamNames") if s.startswith(f"{p}-")]
    left += [f"DynamoDB table {t}" for t in pages("dynamodb", "list_tables", "TableNames") if t.startswith(f"{p}-")]
    left += [f"SNS topic {t['TopicArn'].rsplit(':', 1)[-1]}" for t in pages("sns", "list_topics", "Topics")
             if t["TopicArn"].rsplit(":", 1)[-1].startswith(f"{p}-")]
    left += [f"alarm {a['AlarmName']}" for a in pages("cloudwatch", "describe_alarms", "MetricAlarms",
                                                       AlarmNamePrefix=f"{p}-")]
    iam = sess.client("iam")
    left += [f"IAM role {r['RoleName']}" for page in iam.get_paginator("list_roles").paginate()
             for r in page["Roles"] if r["RoleName"].startswith(f"{p}-")]
    athena, kw, groups = sess.client("athena"), {}, []
    while True:                                  # ListWorkGroups has a NextToken but no paginator
        page = athena.list_work_groups(**kw)
        groups += [w["Name"] for w in page["WorkGroups"]]
        if not page.get("NextToken"):
            break
        kw = {"NextToken": page["NextToken"]}
    left += [f"Athena workgroup {g}" for g in groups if g.startswith(f"{p}-")]
    left += [f"S3 bucket {b['Name']}" for b in sess.client("s3").list_buckets()["Buckets"]
             if b["Name"].startswith(f"{p}-")]
    for item in left:
        log(f"LEFT     {item}")
    log(f"check: {len(left)} left by schedule group and by name prefix {p}-" + (" (empty)" if not left else ""))
    return 1 if left else 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--yes", action="store_true", help="delete; without it, list what would be removed")
    p.add_argument("--check", action="store_true", help="only list what is left")
    args = p.parse_args(argv)
    sess = boto3.session.Session(region_name=REGION)
    if args.check:
        return check(sess)
    steps = clean(sess, args.yes)
    if steps.failed:
        print(f"{len(steps.failed)} steps FAILED: the clean-up is NOT complete. Fix what they say, then run it again")
    elif not args.yes:
        print("Nothing deleted. Run again with --yes to delete these.")
    return 1 if steps.failed else 0


if __name__ == "__main__":
    sys.exit(main())

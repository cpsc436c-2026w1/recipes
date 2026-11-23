"""Remove what ext-maintenance's nightly compaction created for one $PREFIX in ca-central-1, then prove it is gone.

    PREFIX=recipe-yourname uv run python ext-maintenance/clean_up.py           # what it would delete
    PREFIX=recipe-yourname uv run python ext-maintenance/clean_up.py --yes     # delete it
    PREFIX=recipe-yourname uv run python ext-maintenance/clean_up.py --check   # what is left: all empty?

In this order: the two alarms (they bill monthly), the schedule group with its two schedules, the function, its log
group, its two roles (only when the ledger records them `created`) and the alert topic. Then the moved days of
`trip_events_parquet`, which is kept (the landing path may still feed it): for each day whose partition points under
`compacted/trip-events/`, it counts the day through the table, finishes a raw delete left `started`, makes the raw
folder whole (all listed files there: copy back only the `late-*` files; none there: copy back every file of the
day's location as `compacted-<name>`), points the partition at the raw folder and counts again. Different counts:
the partition goes back to `compacted/`, that day's files are kept, and the clean-up stops with an error naming the
day. Only when every day matched are `compacted/trip-events/` and `maintenance/inputs/` deleted. It never deletes the
bucket, `raw/trip-events/`, the run log or the Iceberg tables.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError

HERE = Path(__file__).resolve().parent
# Appended, not put first: a folder of this recipe never hides a module of the same name in another one
# (each extension has its own clean_up.py).
sys.path += [p for p in [str(HERE), str(HERE.parent / "scripts")] if p not in sys.path]
import compact_days as cd  # noqa: E402
import nightly_ops  # noqa: E402
from tags import record, recorded  # noqa: E402

REGION = "ca-central-1"
SCRIPT = "ext-maintenance/clean_up.py --yes"
GONE = {"ResourceNotFoundException", "ResourceNotFound", "NotFoundException", "NotFound", "404", "NoSuchEntity",
        "NoSuchBucket", "EntityNotFoundException"}


class DayMismatch(RuntimeError):
    pass


class Steps:
    def __init__(self, yes: bool):
        self.yes, self.lines = yes, []

    def note(self, kind, what):
        self.lines.append((kind, what))

    def do(self, what, fn=None, **kw) -> bool:
        try:
            if fn and self.yes:
                fn(**kw)
        except (ClientError, BotoCoreError) as err:
            code = getattr(err, "response", {}).get("Error", {}).get("Code", "")
            self.note("absent" if code in GONE else "FAILED", what if code in GONE else f"{what}: {code}: {err}")
            return False
        self.note("removed" if self.yes else "would remove", what)
        return True

    def report(self, log=print) -> int:
        for kind, what in self.lines:
            log(f"{kind:<12} {what}")
        return 1 if any(k == "FAILED" for k, _ in self.lines) else 0


def count_day(athena, cfg: cd.Config, day: str) -> int:
    db, table = cfg.raw_table
    return int(athena.run(f"SELECT count(*) AS n FROM {db}.{table} WHERE dt = '{day}'", cfg.workgroup, db)[0]["n"])


def restore_day(night: cd.Night, moved: dict, steps: Steps) -> None:
    """Point a moved day back at its raw folder with the same rows, or stop with DayMismatch."""
    cfg, s3, glue = night.cfg, night.s3, night.glue
    db, table = cfg.raw_table
    day = moved["moved_day"]
    location = cd.partition_location(glue, db, table, day)
    if not location or cd.COMPACTED not in location:
        return
    before = count_day(night.athena, cfg, day)
    if moved.get("raw_delete") == "started":
        night.delete_raw(moved)                    # finish it first: then the raw folder holds all or none
    listed = json.loads(s3.get_object(Bucket=cfg.bucket, Key=moved["inputs_key"])["Body"].read())["files"]
    raw = cd.listing(s3, cfg.bucket, f"{cd.RAW}dt={day}/")
    lb, lp = cd.key_of(location)
    here = cd.listing(s3, lb, lp)
    if all(k in raw for k in listed):
        back = [k for k in here if k.rsplit("/", 1)[1].startswith("late-")]
        name = lambda k: k.rsplit("/", 1)[1]           # noqa: E731
    else:
        back = sorted(here)
        name = lambda k: f"compacted-{k.rsplit('/', 1)[1]}"   # noqa: E731
    for key in back:
        s3.copy_object(Bucket=cfg.bucket, Key=f"{cd.RAW}dt={day}/{name(key)}", CopySource={"Bucket": lb, "Key": key})
    cd.set_location(glue, db, table, day, cd.raw_folder(cfg.bucket, day))
    after = count_day(night.athena, cfg, day)
    if after != before:
        cd.set_location(glue, db, table, day, location)
        raise DayMismatch(f"{day}: {after} rows through the raw folder, {before} through {location}; the day "
                          f"stays at compacted/ and its files are kept")
    # The day is a raw day again: its record goes, so a later deploy's nightly run does not look for its file list
    # (maintenance/inputs/ is deleted next).
    night.runs.t.delete_item(Key={"job": cd.JOB, "run_id": moved["run_id"]})
    steps.note("restored", f"partition dt={day} at its raw folder ({len(back)} files copied back, {after} rows)")


def clean(sess=None, yes: bool = False, athena=None, log=print) -> int:
    from datetime import datetime, timezone
    sess = sess or boto3.session.Session(region_name=REGION)
    n, steps = nightly_ops.names(), Steps(yes)
    cw, sch, lam, logs, iam, sns = (sess.client(c) for c in ("cloudwatch", "scheduler", "lambda", "logs", "iam", "sns"))
    for alarm in (n["errors"], n["missed"]):
        if cw.describe_alarms(AlarmNames=[alarm])["MetricAlarms"]:
            if steps.do(f"alarm {alarm}", cw.delete_alarms, AlarmNames=[alarm]) and yes:
                record("cloudwatch-alarm", alarm, "deleted", script=SCRIPT)
        else:
            steps.note("absent", f"alarm {alarm}")
    groups = [g["Name"] for g in sch.list_schedule_groups(NamePrefix=n["group"])["ScheduleGroups"]]
    if n["group"] not in groups:
        steps.note("absent", f"schedule group {n['group']}")
    elif steps.do(f"schedule group {n['group']} and its schedules", sch.delete_schedule_group, Name=n["group"]) and yes:
        record("schedule-group", n["group"], "deleted", script=SCRIPT)
    if steps.do(f"function {n['function']}", lam.delete_function, FunctionName=n["function"]) and yes:
        record("lambda-function", n["function"], "deleted", script=SCRIPT)
    if steps.do(f"log group {n['log_group']}", logs.delete_log_group, logGroupName=n["log_group"]) and yes:
        record("log-group", n["log_group"], "deleted", script=SCRIPT)
    for role in (n["role"], n["scheduler_role"]):
        if recorded("iam-role", role) != "created":
            steps.note("kept", f"role {role} ({recorded('iam-role', role) or 'not in the ledger'})")
            continue
        try:
            for policy in iam.list_role_policies(RoleName=role)["PolicyNames"]:
                if yes:
                    iam.delete_role_policy(RoleName=role, PolicyName=policy)
        except ClientError:
            pass
        if steps.do(f"role {role}", iam.delete_role, RoleName=role) and yes:
            record("iam-role", role, "deleted", script=SCRIPT)
    topics = [t["TopicArn"] for t in sns.list_topics()["Topics"] if t["TopicArn"].endswith(f":{n['topic']}")]
    for arn in topics:
        if steps.do(f"topic {n['topic']} and its subscriptions", sns.delete_topic, TopicArn=arn) and yes:
            record("sns-topic", n["topic"], "deleted", script=SCRIPT)
    if not topics:
        steps.note("absent", f"topic {n['topic']}")
    import lake
    bucket = lake.bucket_name(sess)
    cfg = cd.Config(bucket=bucket)
    s3, glue = sess.client("s3"), sess.client("glue")
    try:
        moved = cd.Log(sess.resource("dynamodb").Table(n["run_log"])).moved_days()
    except ClientError:
        moved = []
    if moved and yes:
        night = cd.Night(datetime.now(timezone.utc), cfg, s3, glue, sess.resource("dynamodb").Table(n["run_log"]),
                         athena or cd.Athena(sess.client("athena")), log=lambda *_: None)
        try:
            for m in sorted(moved, key=lambda m: m["moved_day"]):
                restore_day(night, m, steps)
        except DayMismatch as err:
            steps.note("FAILED", f"moved day {err}")
            return steps.report(log)
    elif moved:
        steps.note("would restore", f"{len(moved)} moved days of {'.'.join(cfg.raw_table)} to their raw folders")
    for pfx in (cd.COMPACTED, cd.INPUTS):
        try:
            keys = sorted(cd.listing(s3, bucket, pfx))
        except ClientError:
            keys = []
        if yes:
            cd.delete_keys(s3, bucket, keys)
        steps.note("removed" if yes else "would remove", f"{len(keys)} objects under s3://{bucket}/{pfx}")
    return steps.report(log)


def check(sess=None, log=print) -> int:
    sess = sess or boto3.session.Session(region_name=REGION)
    n, left = nightly_ops.names(), []
    if sess.client("cloudwatch").describe_alarms(AlarmNames=[n["errors"], n["missed"]])["MetricAlarms"]:
        left.append("alarms")
    groups = [g["Name"] for g in sess.client("scheduler").list_schedule_groups(NamePrefix=n["group"])["ScheduleGroups"]]
    left += [f"schedule group {g}" for g in groups if g == n["group"]]
    left += [f"function {f['FunctionName']}" for f in sess.client("lambda").list_functions()["Functions"]
             if f["FunctionName"] == n["function"]]
    left += [f"log group {g['logGroupName']}" for g in
             sess.client("logs").describe_log_groups(logGroupNamePrefix=n["log_group"])["logGroups"]]
    left += [f"topic {t['TopicArn']}" for t in sess.client("sns").list_topics()["Topics"]
             if t["TopicArn"].endswith(f":{n['topic']}")]
    left += [f"role {r['RoleName']}" for r in sess.client("iam").list_roles()["Roles"]
             if r["RoleName"] in (n["role"], n["scheduler_role"]) and recorded("iam-role", r["RoleName"])]
    import lake
    bucket = lake.bucket_name(sess)
    for pfx in (cd.COMPACTED, cd.INPUTS):
        try:
            if cd.listing(sess.client("s3"), bucket, pfx):
                left.append(f"objects under s3://{bucket}/{pfx}")
        except ClientError:
            pass
    for line in left:
        log(f"LEFT   {line}")
    log("nothing of ext-maintenance's nightly compaction is left" if not left else f"{len(left)} kinds left")
    return 1 if left else 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--yes", action="store_true", help="delete; without it, only list what would go")
    p.add_argument("--check", action="store_true", help="list what is left")
    args = p.parse_args(argv)
    if args.check:
        return check()
    code = clean(yes=args.yes)
    if not args.yes:
        print("Nothing deleted. Run again with --yes to delete these.")
    return code


if __name__ == "__main__":
    sys.exit(main())

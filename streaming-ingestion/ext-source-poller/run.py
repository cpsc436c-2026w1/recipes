"""ext-source-poller: a scheduled function loads each slot file of a partner's feed onto the stream once.

    uv run python ext-source-poller/run.py --target local        # offline, in moto: a whole simulated day
    uv run python ext-source-poller/run.py --target aws          # in a sandbox: the poller and its schedule
    uv run python ext-source-poller/run.py stop                  # delete the schedule group: the poller stops

`--target aws` needs the main scenario's stream and consumer (`main/run.py --target aws --keep`: the stream
`$PREFIX-updates` is borrowed, not created here) and the lake bucket (`scripts/lake.py up`, borrowed or created).
It creates the slot table `$PREFIX-feed-slots`, the run log `$PREFIX-runs` if it is missing (scripts/run_log.py),
the function `$PREFIX-feed-poller` with its role and log group, and the schedule `$PREFIX-feed-schedule`
(rate(15 minutes)) in the schedule group `$PREFIX-source-poller`, with its role. Every create is tagged
(scripts/tags.py) and written to state/ledger.jsonl. Then publish the feed: `ext-source-poller/feed.py --to aws
--live`.

`--target local` runs the same poller code in moto every 15 minutes of a simulated day (00:15 to 02:00 the next
day), with the main consumer reading the stream, and prints the day's slot list.

Settings and their reasons: README "Decisions".
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
for folder in (HERE, ROOT / "main", ROOT / "scripts", ROOT / "sim", ROOT / "src"):
    sys.path.insert(0, str(folder))

import infra  # noqa: E402
from tags import record, tags  # noqa: E402

TIMEOUT_S = 300
MEMORY_MB = 128
RATE = "rate(15 minutes)"
SCHEDULER_TRUST = lambda account: {"Version": "2012-10-17", "Statement": [{  # noqa: E731
    "Effect": "Allow", "Principal": {"Service": "scheduler.amazonaws.com"}, "Action": "sts:AssumeRole",
    "Condition": {"StringEquals": {"aws:SourceAccount": account}}}]}


def _map(pairs) -> dict:
    return {p["Key"]: p["Value"] for p in pairs} if isinstance(pairs, list) else dict(pairs)


def create_slots_table(ddb, prefix: str) -> str:
    n = infra.names(prefix)
    arn = infra.create_table(ddb, n["slots_table"], "slot_day", sort_key="slot_start", tags=_map(tags("dynamodb")))
    record("dynamodb-table", n["slots_table"], arn=arn, script="ext-source-poller/run.py", clean="clean_up.py")
    return arn


def create(session, prefix: str, bucket: str) -> dict:
    import run_log

    n = infra.names(prefix)
    region, acct = session.region_name, infra.account_id(session)
    ddb, iam, lam = session.client("dynamodb"), session.client("iam"), session.client("lambda")
    kinesis, scheduler = session.client("kinesis"), session.client("scheduler")
    script, clean = "ext-source-poller/run.py", "clean_up.py"
    try:
        stream_arn = kinesis.describe_stream_summary(StreamName=n["stream"])["StreamDescriptionSummary"]["StreamARN"]
    except kinesis.exceptions.ResourceNotFoundException:
        raise SystemExit(f"the stream {n['stream']} is not there: run `uv run python main/run.py --target aws "
                         f"--keep` first (the poller sends to the main scenario's stream)") from None
    record("kinesis-stream", n["stream"], "borrowed", arn=stream_arn, script=script)
    slots_arn = create_slots_table(ddb, prefix)
    log_name = run_log.ensure_run_log(session)
    log_arn = ddb.describe_table(TableName=log_name)["Table"]["TableArn"]

    role_arn = infra.create_role(iam, n["poller_role"], [
        {"Effect": "Allow", "Action": ["s3:GetObject"], "Resource": f"arn:aws:s3:::{bucket}/feed/*"},
        {"Effect": "Allow", "Action": ["dynamodb:Query", "dynamodb:PutItem", "dynamodb:UpdateItem"],
         "Resource": slots_arn},
        {"Effect": "Allow", "Action": ["dynamodb:PutItem", "dynamodb:DeleteItem"], "Resource": log_arn},
        {"Effect": "Allow", "Action": ["kinesis:PutRecords"], "Resource": stream_arn},
    ], region, acct, n["poller"], tags=_map(tags("iam")))
    record("iam-role", n["poller_role"], arn=role_arn, script=script, clean=clean)
    infra.create_log_group(session.client("logs"), n["poller"], tags=tags("logs"))
    record("log-group", f"/aws/lambda/{n['poller']}", script=script, clean=clean)
    env = {"FEED_URL": f"s3://{bucket}/feed/", "SLOTS_TABLE": n["slots_table"], "RUN_LOG": log_name,
           "STREAM": n["stream"]}
    fn_arn = infra.create_function(lam, n["poller"], role_arn, infra.zip_files({"poller.py": HERE / "poller.py"}),
                                   "poller.handler", env, timeout=TIMEOUT_S, memory=MEMORY_MB, tags=tags("lambda"))
    # a scheduled run is not retried by Lambda: the next run, 15 min later, takes over what this one left
    lam.put_function_event_invoke_config(FunctionName=n["poller"], MaximumRetryAttempts=0)
    record("lambda-function", n["poller"], arn=fn_arn, script=script, clean=clean)

    sched_role = scheduler_role(iam, n["scheduler_role"], fn_arn, acct)
    record("iam-role", n["scheduler_role"], arn=sched_role, script=script, clean=clean)
    try:
        scheduler.create_schedule_group(Name=n["schedule_group"], Tags=tags("scheduler"))
        record("schedule-group", n["schedule_group"], script=script, clean=clean)
    except scheduler.exceptions.ConflictException:
        record("schedule-group", n["schedule_group"], "borrowed", script=script)
    target = {"Arn": fn_arn, "RoleArn": sched_role, "RetryPolicy": {"MaximumRetryAttempts": 0},
              "Input": json.dumps({"scheduled_time": "<aws.scheduler.scheduled-time>"})}
    kw = {"Name": n["schedule"], "GroupName": n["schedule_group"], "ScheduleExpression": RATE,
          "FlexibleTimeWindow": {"Mode": "OFF"}, "Target": target, "State": "ENABLED"}
    for attempt in range(10):
        try:
            try:
                scheduler.create_schedule(**kw)
            except scheduler.exceptions.ConflictException:
                scheduler.update_schedule(**kw)
            break
        except scheduler.exceptions.ValidationException:
            if attempt == 9:
                raise
            time.sleep(3)              # a role made seconds ago may not be assumable by the scheduler yet
    return {"function": fn_arn, "slots_table": n["slots_table"], "run_log": log_name, "feed": env["FEED_URL"]}


def scheduler_role(iam, name: str, function_arn: str, account: str) -> str:
    """The schedule's role: trusted by EventBridge Scheduler from this account only, allowed to call one function."""
    if os.environ.get("SCHEDULER_ROLE_ARN"):
        return os.environ["SCHEDULER_ROLE_ARN"]
    try:
        arn = iam.create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps(SCHEDULER_TRUST(account)),
                              Tags=tags("iam"))["Role"]["Arn"]
    except iam.exceptions.EntityAlreadyExistsException:
        arn = iam.get_role(RoleName=name)["Role"]["Arn"]
    iam.put_role_policy(RoleName=name, PolicyName="least-privilege", PolicyDocument=json.dumps({
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Allow", "Action": ["lambda:InvokeFunction"], "Resource": function_arn}]}))
    return arn


def stop(session, prefix: str) -> None:
    n = infra.names(prefix)
    sched = session.client("scheduler")
    try:
        sched.delete_schedule_group(Name=n["schedule_group"])
        print(f"schedule group {n['schedule_group']} deleted, with its schedule: the poller is not called again")
    except sched.exceptions.ResourceNotFoundException:
        print(f"schedule group {n['schedule_group']}: not there")


# ---------- offline: the whole day in moto

def simulate_day(session, prefix: str, start: datetime, end: datetime, up_to: datetime | None = None,
                 every: timedelta = timedelta(minutes=15), consume: bool = True, data_dir: Path | None = None,
                 clock_of=None) -> dict:
    """Publish SIM01 under feed/ of the lake bucket and run the poller every 15 minutes from `start` to `end`;
    before each run the manifest lists what the partner had published by then (or by `up_to`). The main consumer
    reads the stream afterwards (main/local_poller.py). Returns the summaries and what was printed."""
    import contextlib
    import io

    import consumer
    import feed
    import lake
    import local_poller
    import poller
    import run_log

    n = infra.names(prefix)
    ddb, s3, kinesis = session.client("dynamodb"), session.client("s3"), session.client("kinesis")
    lake.up(session)
    bucket = lake.bucket_name(session)
    slots = feed.day_slots(data_dir)
    feed.publish(feed.s3_put(s3, bucket), slots, up_to or start)
    runs, printed, t = [], io.StringIO(), start
    while t <= end:
        feed.publish(feed.s3_put(s3, bucket), slots, min(t, up_to) if up_to else t, files=False)
        f = poller.Feed(f"s3://{bucket}/feed/", s3=s3)
        with contextlib.redirect_stdout(printed):
            runs.append(poller.run(t, ddb=ddb, kinesis=kinesis, feed=f, slots_table=n["slots_table"],
                                   run_log=run_log.run_log_name(), stream=n["stream"], sleep=lambda s: None,
                                   wall=(clock_of(t) if clock_of else (lambda t=t: t + timedelta(seconds=5)))))
        t += every
    stored = None
    if consume:
        with contextlib.redirect_stdout(io.StringIO()):
            local_poller.drain(kinesis, n["stream"], lambda e: consumer.handler(e, ddb=ddb, table=n["table"]),
                               batch_size=1000)
        stored = count_items(ddb, n["table"])
    return {"runs": runs, "printed": printed.getvalue(), "stored": stored, "slots": slots}


def count_items(ddb, table: str) -> int:
    """Every item: a Scan reads at most 1 MB a page, so a COUNT follows every page."""
    total, kw = 0, {"TableName": table, "Select": "COUNT", "ConsistentRead": True}
    while True:
        page = ddb.scan(**kw)
        total += page["Count"]
        if "LastEvaluatedKey" not in page:
            return total
        kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def make_local(session, prefix: str) -> None:
    import run_log

    n = infra.names(prefix)
    infra.create_stream(session.client("kinesis"), n["stream"], poll_s=0.01)
    infra.create_table(session.client("dynamodb"), n["table"], "update_id", poll_s=0.01)
    create_slots_table(session.client("dynamodb"), prefix)
    run_log.ensure_run_log(session)


def run_local(prefix: str) -> int:
    from moto import mock_aws

    os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
    os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
    os.environ.setdefault("LEDGER", str(ROOT / "state" / "ledger-local.jsonl"))
    import boto3
    import slots as slot_list

    with mock_aws():
        session = boto3.Session(region_name="ca-central-1")
        make_local(session, prefix)
        start = datetime(2026, 11, 22, 0, 15, tzinfo=timezone.utc)
        got = simulate_day(session, prefix, start, start + timedelta(hours=25, minutes=45))
        published = [s for s in got["slots"] if s["published_time"]]
        loaded = sum(r["loaded"] for r in got["runs"])
        late = sum(r["late"] for r in got["runs"])
        trips = sum(s["trips"] for s in published)
        print(f"{len(got['runs'])} runs from {start:%Y-%m-%d %H:%M} to 02:00 the next day: {loaded} slots loaded, "
              f"{late} of them late; {got['stored']} updates stored of the {trips} trips in the "
              f"{len(published)} published files")
        status, body = slot_list.get_slots({"day": "2026-11-22"}, session.client("dynamodb"),
                                           infra.names(prefix)["slots_table"],
                                           datetime(2026, 11, 23, 0, 46, tzinfo=timezone.utc))
        states = [s["slot_state"] for s in body["slots"]]
        print(f"GET /api/slots?day=2026-11-22 at 00:46 the next day: {status}, {len(states)} slots: "
              + ", ".join(f"{states.count(k)} {k}" for k in ("loaded", "late", "missing", "not due") if k in states))
        print("missing: " + ", ".join(s["slot_start"] for s in body["slots"] if s["slot_state"] == "missing"))
        return 0 if (loaded, late, got["stored"]) == (95, 1, trips) else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("action", nargs="?", choices=["run", "stop"], default="run")
    p.add_argument("--target", choices=["local", "aws"], default="local")
    args = p.parse_args(argv)
    prefix = os.environ.get("PREFIX", "recipe")
    if args.action == "run" and args.target == "local":
        return run_local(prefix)
    import boto3
    import lake

    session = boto3.Session(region_name=os.environ.get("AWS_REGION", "ca-central-1"))
    if args.action == "stop":
        stop(session, prefix)
        return 0
    lake.up(session)
    made = create(session, prefix, lake.bucket_name(session))
    print(f"created: {made['function']} every 15 minutes, slots in {made['slots_table']}, feed {made['feed']}")
    print("next: uv run python ext-source-poller/feed.py --to aws --live --slots 8   (2 h 15 min; 96 for the day)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

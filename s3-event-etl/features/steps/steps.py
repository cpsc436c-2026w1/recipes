"""Steps of features/s3_event_etl.feature."""
from __future__ import annotations

import contextlib
import io
import json
import os
import time
from datetime import datetime, timedelta, timezone

import boto3
from behave import given, then, when

import check
import control
import etl_job
import local_load
import sweep as sweep_mod

AT = datetime(2026, 11, 23, 23, 0, tzinfo=timezone.utc)      # a fixed "now" for the limits
DAY = "2026-11-23"
seq_counter = iter(range(0x10000, 0x10000000))


def c(name):
    return boto3.client(name, region_name="ca-central-1")


def sess():
    return boto3.session.Session(region_name="ca-central-1")


def quiet(context, fn, *a, **kw):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        result = fn(*a, **kw, log=print)
    context.printed += out.getvalue()
    return result


def next_seq() -> str:
    return f"{next(seq_counter):X}"


def raw_keys(context) -> list[str]:
    out = []
    for page in c("s3").get_paginator("list_objects_v2").paginate(Bucket=context.bucket, Prefix=check.RAW):
        out += [o["Key"] for o in page.get("Contents", [])]
    return sorted(out)


def staged_keys(context) -> list[str]:
    return etl_job._keys(c("s3"), context.bucket, f"{etl_job.STAGE}dt={DAY}/")


@given("the lake bucket, the run log and the alerts topic of PREFIX {p} exist")
def step_base(context, p):
    if context.live:
        return
    import lake
    import run_log
    control.now = lambda: AT
    lake.up(sess(), log=lambda *a: None)
    context.bucket = lake.bucket_name(sess())
    run_log.ensure_run_log(sess())
    c("sns").create_topic(Name=f"{p}-etl-alerts")


def land_sim01(context) -> None:
    import replay
    with contextlib.redirect_stdout(io.StringIO()):
        replay.main(["SIM01", "--run-id", "run1", "--to", "s3", "--target", context.bucket,
                     "--evidence", str(context.tmp / "evidence")])
    context.raw = raw_keys(context)
    assert len(context.raw) == 110, len(context.raw)      # one object per minute of received_time, 09:00 to 10:49


def check_all(context, start=False) -> None:
    context.seqs = {}
    for key in raw_keys(context):
        context.seqs[key] = next_seq()
        quiet(context, check.check_object, context.bucket, key, context.seqs[key], sess(), start=start)


def obj(context, key: str) -> str:
    return c("s3").get_object(Bucket=context.bucket, Key=key)["Body"].read().decode()


def put(context, key: str, text: str) -> None:
    c("s3").put_object(Bucket=context.bucket, Key=key, Body=text.encode())


def event(context, key: str, seq: str) -> dict:
    return {"Records": [{"s3": {"bucket": {"name": context.bucket}, "object": {"key": key, "sequencer": seq}}}]}


# ---- R10

@given("the object {key} holds these deliveries:")
def step_object(context, key):
    lines = []
    for r in context.table:
        d = {h: r[h] for h in context.table.headings}
        d["pickup_zone_id"] = int(d["pickup_zone_id"])
        lines.append(json.dumps(d, sort_keys=True))
    context.key, context.lines = key, lines
    put(context, key, "".join(x + "\n" for x in lines))


@when("S3 sends the object's created event to the check function")
def step_event(context):
    context.seq = next_seq()
    os.environ["START_ON_STAGE"] = "0"
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        context.result = check.handler(event(context, context.key, context.seq))
    context.printed += out.getvalue()


@then("stage/trip-events/dt={day}/{stem}-<sequencer>.jsonl holds the same {n:d} lines in the same order")
def step_staged(context, day, stem, n):
    key = f"stage/trip-events/dt={day}/{stem}-{context.seq.zfill(32)}.jsonl"
    assert obj(context, key).splitlines() == context.lines and len(context.lines) == n


@then("the run log item etl-object for the key has good_lines {good:d} and bad_lines {bad:d}")
def step_item(context, good, bad):
    item = c("dynamodb").get_item(TableName="recipe-runs", Key={"job": {"S": "etl-object"},
                                                                 "run_id": {"S": context.key}})["Item"]
    assert item["good_lines"]["N"] == str(good) and item["bad_lines"]["N"] == str(bad) and "checked_at" in item


# ---- data sets

@given('the simulated data set SIM02 "bad-lines" has been landed in the lake bucket')
def step_sim02(context):
    land_sim01(context)
    events = {}
    for k in context.raw:
        for line in obj(context, k).splitlines():
            events.setdefault(json.loads(line)["event_id"], 0)
            events[json.loads(line)["event_id"]] += 1
    key = next(k for k in context.raw if "T0904Z" in k)
    lines = obj(context, key).splitlines()
    once = [i for i, ln in enumerate(lines) if events[json.loads(ln)["event_id"]] == 1][:3]
    for i, how in zip(once, ("tip", "event_time", "trip_id")):
        d = json.loads(lines[i])
        if how == "tip":
            d["tip"] = 2.5
        elif how == "event_time":
            d["event_time"] = d["event_time"].removesuffix("Z").split(".")[0]
        else:
            d.pop("trip_id")
        lines[i] = json.dumps(d, sort_keys=True)
    put(context, key, "".join(x + "\n" for x in lines))


@when("every object's event has been handled and the load has run")
def step_handled_and_loaded(context):
    check_all(context)
    quiet(context, control.maybe_start, context.bucket, sess(), AT)


@then('quarantine/trip-events-rows/dt={day}/ holds 1 object of 3 lines, with line numbers and the reasons "{a}", "{b}" and "{r3}"')
def step_quarantine(context, day, a, b, r3):
    keys = etl_job._keys(c("s3"), context.bucket, f"quarantine/trip-events-rows/dt={day}/")
    assert len(keys) == 1, keys
    rows = [json.loads(x) for x in obj(context, keys[0]).splitlines()]
    assert [r["reason"] for r in rows] == [a, b, r3] and all(isinstance(r["line_no"], int) for r in rows), rows


@then("table trip_events_clean has {n:d} rows")
def step_rows(context, n):
    assert local_load.count() == n, local_load.count()


@then("table trip_events_clean has {n:d} rows, one per event_id")
def step_rows_distinct(context, n):
    step_rows(context, n)          # local_load.count() also asserts one row per event_id


@given('the simulated data set {sim} "{name}" has been landed and checked')
def step_landed_checked(context, sim, name):
    land_sim01(context)
    if sim == "SIM03":
        again = next(k for k in context.raw if "T0930Z" in k)
        context.again = again.replace(".jsonl", "-again.jsonl")
        put(context, context.again, obj(context, again))
    check_all(context)
    if sim == "SIM03":
        context.repeat_key = next(k for k in context.raw if "T0930Z" in k)
        quiet(context, check.check_object, context.bucket, context.repeat_key, context.seqs[context.repeat_key],
              sess(), start=False)


@when("the ETL job runs")
def step_job_runs(context):
    run = quiet(context, control.maybe_start, context.bucket, sess(), AT)
    assert run, context.printed
    quiet(context, sweep_mod.follow_runs, sess(), AT)


@then("lake/etl-state/dt={day}.txt lists the {n:d} staged keys")
def step_manifest(context, day, n):
    done = etl_job.manifest(c("s3"), context.bucket, day)
    assert len(done) == n and set(done) == set(staged_keys(context)) and set(done.values()) == {"merged"}, done


@then("the run log counts 1 repeat event, for the 09:30 object")
def step_repeat(context):
    items = c("dynamodb").query(TableName="recipe-runs", KeyConditionExpression="job = :j",
                                ExpressionAttributeValues={":j": {"S": "etl-object"}})["Items"]
    repeats = {i["run_id"]["S"]: int(i["repeats"]["N"]) for i in items if int(i["repeats"]["N"])}
    assert repeats == {context.repeat_key: 1}, repeats


@then("the staged object of the key …-again.jsonl added 0 rows")
def step_again(context):
    con = local_load.connect()
    n = con.execute("SELECT count(*) FROM trip_events_clean WHERE source_key LIKE '%-again-%'").fetchone()[0]
    con.close()
    assert n == 0 and any("-again-" in k for k in staged_keys(context)), n


# ---- R50, R60

def put_run(minutes_ago: int, state: str) -> None:
    started = control.iso(AT - timedelta(minutes=minutes_ago))
    control.record_run(c("dynamodb"), started, state, started_at=started)


@given("the last run started {minutes:d} minutes ago and is {state}, and {runs:d} runs have started today")
def step_runs(context, minutes, state, runs):
    for i in range(runs - 1, 0, -1):
        put_run(minutes + 10 * i, "finished")
    put_run(minutes, state)


@when("an object is staged")
def step_staged_one(context):
    key = f"raw/trip-events/dt={DAY}/0905-one.jsonl"
    put(context, key, json.dumps({"delivery_id": "dl-1", "event_type": "trip_requested", "event_id": "ev-1",
                                  "trip_id": "t-1", "pickup_zone_id": 48, "event_time": "2026-11-23T09:05:10Z",
                                  "received_time": "2026-11-23T09:05:11Z"}) + "\n")
    quiet(context, check.check_object, context.bucket, key, next_seq(), sess(), start=True)


def no_start(context, says=""):
    assert not context.calls.of("StartJobRun"), context.printed
    assert says in context.printed, context.printed


@then("a run starts")
def step_starts(context):
    assert len(context.calls.of("StartJobRun")) == 1, context.printed


@then("no run starts, and the sweep will start one")
def step_no_start_gap(context):
    no_start(context, "the sweep will start one")


@then("no run starts")
def step_no_start(context):
    no_start(context, "a run is running")


@then("no run starts until the next UTC day")
def step_no_start_day(context):
    no_start(context, "until the next UTC day")


@when("the first run fails after its commit and the sweep runs")
def step_fail_first(context):
    context.glue.fail_next = True
    context.run_id = quiet(context, control.maybe_start, context.bucket, sess(), AT)
    quiet(context, sweep_mod.sweep, context.bucket, sess(), AT)


@then('the run log records the run failed with failed_step "{step}"')
def step_failed(context, step):
    items = control.runs(c("dynamodb"))
    run = next(i for i in items if i["run_id"]["S"] == context.run_id)
    assert run["run_state"]["S"] == "failed" and run["failed_step"]["S"] == step, run


@then("topic recipe-etl-alerts received 1 message naming the run")
def step_alert(context):
    sent = context.calls.of("Publish")
    assert len(sent) == 1 and context.run_id in sent[0]["Message"] and "recipe-etl-alerts" in sent[0]["TopicArn"]


@when("the next run starts")
def step_next_run(context):
    later = AT + timedelta(minutes=361)
    control.now = lambda: later
    assert quiet(context, control.maybe_start, context.bucket, sess(), later), context.printed
    quiet(context, sweep_mod.follow_runs, sess(), later)


@given("the last 3 runs of the job failed")
def step_three_failed(context):
    for m in (1000, 800, 600):
        put_run(m, "failed")


@when("the sweep finds staged objects waiting")
def step_sweep_waiting(context):
    key = f"raw/trip-events/dt={DAY}/0905-one.jsonl"
    put(context, key, json.dumps({"delivery_id": "dl-1", "event_type": "trip_requested", "event_id": "ev-1",
                                  "trip_id": "t-1", "pickup_zone_id": 48, "event_time": "2026-11-23T09:05:10Z",
                                  "received_time": "2026-11-23T09:05:11Z"}) + "\n")
    quiet(context, check.check_object, context.bucket, key, next_seq(), sess(), start=False)
    quiet(context, sweep_mod.sweep, context.bucket, sess(), AT)


@then('no run starts and the run is recorded skipped with error_text "{text}"')
def step_held(context, text):
    assert not context.calls.of("StartJobRun")
    last = control.runs(c("dynamodb"))[-1]
    assert last["run_state"]["S"] == "skipped" and last["error_text"]["S"] == text, last


@when("etl.py resume runs and the sweep runs again")
def step_resume(context):
    quiet(context, control.resume, sess())
    quiet(context, sweep_mod.sweep, context.bucket, sess(), AT)


# ---- R70, R80, R90

def etl_up(context):
    import etl
    quiet(context, etl.up, sess(), deps=False, sleep=lambda s: None)


@when("etl.py up creates the job")
def step_up(context):
    etl_up(context)


@then("job recipe-etl has worker type G.1X, 2 workers, Timeout 10, MaxRetries 0, MaxConcurrentRuns 1 and run queuing off")
def step_job(context):
    (job,) = context.calls.of("CreateJob")
    assert (job["WorkerType"], job["NumberOfWorkers"], job["Timeout"], job["MaxRetries"],
            job["ExecutionProperty"]["MaxConcurrentRuns"], job["JobRunQueuingEnabled"]) == ("G.1X", 2, 10, 0, 1, False)


@given("ENGINE is athena")
def step_athena(context):
    os.environ["ENGINE"] = "athena"


@when("the sweep runs")
def step_sweep(context):
    quiet(context, sweep_mod.sweep, context.bucket, sess(), AT)


@then("it started 1 query in workgroup recipe-wg, a MERGE INTO trip_events_clean")
def step_query(context):
    merges = [q for q in context.calls.of("StartQueryExecution") if q["QueryString"].startswith("MERGE INTO")]
    assert len(merges) == 1 and merges[0]["WorkGroup"] == "recipe-wg", merges
    assert "trip_events_clean t USING" in merges[0]["QueryString"] and "WHEN NOT MATCHED THEN INSERT" in \
        merges[0]["QueryString"]
    assert not context.calls.of("StartJobRun")


@given("the bucket's notification configuration has the entries quality-gate and recipe-s3-event-etl")
def step_entries(context):
    c("sqs").create_queue(QueueName="quality-gate")
    c("s3").put_bucket_notification_configuration(Bucket=context.bucket, NotificationConfiguration={
        "QueueConfigurations": [{"Id": "quality-gate", "Events": ["s3:ObjectCreated:*"],
                                 "QueueArn": "arn:aws:sqs:ca-central-1:123456789012:quality-gate",
                                 "Filter": {"Key": {"FilterRules": [{"Name": "prefix", "Value": "incoming/"}]}}}]})
    etl_up(context)
    config = c("s3").get_bucket_notification_configuration(Bucket=context.bucket)
    ids = [e["Id"] for k in ("QueueConfigurations", "LambdaFunctionConfigurations") for e in config.get(k, [])]
    assert sorted(ids) == ["quality-gate", "recipe-s3-event-etl"], ids
    put(context, "raw/trip-events/dt=2026-11-23/x.jsonl", "{}\n")
    put(context, "stage/trip-events/dt=2026-11-23/x-1.jsonl", "{}\n")


@when("clean_up.py --yes runs")
def step_clean(context):
    import importlib.util
    spec = importlib.util.spec_from_file_location("clean_up_t", os.path.join(
        os.path.dirname(__file__), "..", "..", "clean_up.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    context.exit_code = quiet(context, module.clean, sess(), True)


@then("the bucket's notification configuration has only the entry quality-gate")
def step_only_gate(context):
    config = c("s3").get_bucket_notification_configuration(Bucket=context.bucket)
    ids = [e["Id"] for k in ("QueueConfigurations", "LambdaFunctionConfigurations") for e in config.get(k, [])]
    assert ids == ["quality-gate"] and context.exit_code == 0, (ids, context.printed)


@then("functions recipe-etl-check and recipe-etl-sweep, job recipe-etl and the prefix stage/trip-events/ are gone")
def step_gone(context):
    names = [f["FunctionName"] for f in c("lambda").list_functions()["Functions"]]
    assert "recipe-etl-check" not in names and "recipe-etl-sweep" not in names, names
    assert "recipe-etl" not in c("glue").list_jobs()["JobNames"]
    assert not etl_job._keys(c("s3"), context.bucket, "stage/trip-events/")


@then("the bucket and raw/trip-events/ are still there")
def step_kept(context):
    c("s3").head_bucket(Bucket=context.bucket)
    assert raw_keys(context) == ["raw/trip-events/dt=2026-11-23/x.jsonl"]


# ---- @live

@given('the simulated data set SIM01 "rush-hour" has been landed in the lake bucket in AWS')
def step_live_land(context):
    import lake
    import subprocess
    import sys
    context.bucket = lake.bucket_name(sess())
    context.run_id = f"live{int(time.time())}"
    subprocess.run([sys.executable, "sim/replay.py", "SIM01", "--run-id", context.run_id, "--to", "s3",
                    "--target", context.bucket, "--shift-to", "now"], check=True)


@when("every object's event has been handled")
def step_live_wait(context):
    deadline = time.time() + 600
    while time.time() < deadline:
        staged = [o for p in c("s3").get_paginator("list_objects_v2").paginate(
            Bucket=context.bucket, Prefix="stage/trip-events/") for o in p.get("Contents", [])
            if context.run_id in o["Key"]]
        if len(staged) >= 60:
            break
        time.sleep(15)
    context.staged = staged


@then("60 staged objects exist, and the p95 of their LastModified minus their raw object's is under 2 min")
def step_live_latency(context):
    raw = {o["Key"].rsplit("/", 1)[1].removesuffix(".jsonl"): o["LastModified"] for p in
           c("s3").get_paginator("list_objects_v2").paginate(Bucket=context.bucket, Prefix="raw/trip-events/")
           for o in p.get("Contents", []) if context.run_id in o["Key"]}
    lags = sorted((o["LastModified"] - raw[o["Key"].rsplit("/", 1)[1].rsplit("-", 1)[0]]).total_seconds()
                  for o in context.staged)
    print(f"validation latency: median {lags[len(lags) // 2]:.1f} s, p95 {lags[int(len(lags) * 0.95) - 1]:.1f} s")
    assert len(lags) == 60 and lags[int(len(lags) * 0.95) - 1] < 120, lags

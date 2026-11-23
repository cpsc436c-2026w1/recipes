"""Steps of the simulated-runs scenarios. Every Then compares with a number the scenario states, which the answer
computed from _truth/ of the data set also holds; none compares with the system's own count."""
from __future__ import annotations

import configparser
import filecmp
import json
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import boto3
from behave import given, then, use_step_matcher, when

import check
import generate
import live_check
import replay
import truth
from rides import pipeline
from rides.events import EVENT, LineError, validate_line
from run_log import ensure_run_log

ROOT = Path(__file__).resolve().parent.parent.parent
REGION = "ca-central-1"
START = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)


class FakeTime:
    """A clock that moves only when the replayer sleeps: an hour at speed 60 takes no time in a scenario."""

    def __init__(self):
        self.t = 0.0

    def clock(self):
        return self.t

    def wall(self):
        return START + timedelta(seconds=self.t)

    def sleep(self, s):
        self.t += s


def sess():
    return boto3.session.Session(region_name=REGION)


def jsonl(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def zones_of(text):
    """'304, 191 and 342' -> [304, 191, 342]"""
    return [int(x) for x in text.replace(" and ", ", ").split(", ")]


# --- the generator -------------------------------------------------------------------------------------------------

use_step_matcher("re")


@when(r'the generator runs with the data set (?P<sid>SIM\d+) "(?P<name>[^"]+)" and the seed (?P<seed>\d+)')
def gen_now(context, sid, name, seed):
    context.out = context.tmp / "sim" / "data"
    context.sid, context.seed = sid, int(seed)
    generate.generate(sid, int(seed), context.out)


@given(r'the generator has run with the data set (?P<sid>SIM\d+)(?: "(?P<name>[^"]+)")? and the seed (?P<seed>\d+)')
def gen_before(context, sid, seed, name=None):
    gen_now(context, sid, name, seed)


@when(r'the generator runs again with the data set (?P<sid>SIM\d+) "(?P<name>[^"]+)" and the seed (?P<seed>\d+) '
      r'into another folder')
def gen_again(context, sid, name, seed):
    context.other = context.tmp / "other"
    generate.generate(sid, int(seed), context.other)


@given(r'the simulated data set (?P<sid>SIM\d+) "(?P<name>[^"]+)" was replayed with run id "(?P<run>[^"]+)" '
       r'to "(?P<to>[^"]+)"')
def replayed_before(context, sid, name, run, to):
    context.sid = sid
    context.received = []
    replay.replay(replay.load_deliveries(sid, context.data), replay.LocalSender(context.received.extend), run)


@given(r'the simulated data set (?P<sid>SIM\d+) "(?P<name>[^"]+)"')
def data_set(context, sid, name):
    context.sid = sid
    assert generate.CONFIG[sid]["name"] == name, f"{sid} is {generate.CONFIG[sid]['name']!r} in sim/datasets.toml"


use_step_matcher("parse")


@then("sim/data/{sid}/{file} has {n:d} lines")
def has_lines(context, sid, file, n):
    assert len((context.out / sid / file).read_text().splitlines()) == n


@then("the delivery files of {sid} hold {n:d} lines in {k:d} folders trip-events/dt={day}/hour=<hour>/")
def delivery_files(context, sid, n, k, day):
    files = generate.delivery_files(context.out / sid)
    assert len(files) == k and all(f.relative_to(context.out / sid).parts[:2] == ("trip-events", f"dt={day}")
                                   for f in files), files
    assert len(generate.delivery_lines(context.out / sid)) == n


@then("the two folders hold the same files with the same bytes")
def same_bytes(context):
    a, b = context.out / context.sid, context.other / context.sid
    names = sorted(str(p.relative_to(a)) for p in a.rglob("*") if p.is_file())
    assert names == sorted(str(p.relative_to(b)) for p in b.rglob("*") if p.is_file()) and len(names) == 5, names
    match, mismatch, errors = filecmp.cmpfiles(a, b, names, shallow=False)
    assert not mismatch and not errors, mismatch


@when("_truth/faults.jsonl is read")
def read_faults(context):
    context.faults = truth.by_kind(truth.read_faults(context.out / context.sid))


@then('it lists {twice:d} "twice", {late:d} "late", {never:d} "never" and {changed:d} "changed"')
def lists_faults(context, twice, late, never, changed):
    got = {k: len(v) for k, v in context.faults.items()}
    assert got == {"twice": twice, "late": late, "never": never, "changed": changed}, got


def deliveries(folder_):
    return [json.loads(line) for line in generate.delivery_lines(folder_)]


@then('each "late" delivery is a line of the delivery files with a received_time {low:d} to {high:d} minutes after '
      'its event_time')
def late_by(context, low, high):
    lines = {d["delivery_id"]: d for d in deliveries(context.out / context.sid)}
    for did in context.faults["late"]:
        d = lines[did]
        delay = (replay.parse(d["received_time"]) - replay.parse(d["event_time"])).total_seconds()
        assert low * 60 <= delay <= high * 60, (did, delay)


def _fault_events(folder_, faults):
    lines = {d["delivery_id"]: d for d in deliveries(folder_)}
    return {kind: sorted(lines[i]["event_id"] for i in faults[kind]) for kind in ("twice", "late")}


@then('its "twice" and "late" deliveries carry the same events as those of SIM01')
def same_as_sim01(context):
    generate.generate("SIM01", context.seed, context.out)
    sim01 = truth.by_kind(truth.read_faults(context.out / "SIM01"))
    assert _fault_events(context.out / "SIM04", context.faults) == _fault_events(context.out / "SIM01", sim01)


@then('none of its "never" or "changed" trips is a trip that SIM01\'s faults touch')
def untouched(context):
    events = {e["event_id"]: e for e in jsonl(context.out / "SIM04" / "_truth" / "events.jsonl")}
    touched = {events[e]["trip_id"] for kind in _fault_events(context.out / "SIM04", context.faults).values()
               for e in kind}
    mine = {events[e]["trip_id"] for e in context.faults["never"] + context.faults["changed"]}
    assert len(mine) == 5 and not mine & touched, (mine, touched)


@when("the answer is computed from _truth/")
def read_truth(context):
    context.truth = truth.answer(context.out / context.sid)


@then("it holds {events:d} events and {trips:d} trips, requested per zone {counts} for zones {zones}")
def truth_holds(context, events, trips, counts, zones):
    t = context.truth
    want = dict(zip(map(str, zones_of(zones)), zones_of(counts)))
    assert (t["events"], t["trips"], t["requested_per_zone"]) == (events, trips, want), t["requested_per_zone"]


@then("{n:d} trips are completed")
def completed(context, n):
    assert context.truth["completed"] == n


def first_fares(path, changed_ids):
    """trip_id -> (the first trip_completed's fare, the correction's fare) for each corrected trip."""
    events = jsonl(path)
    by_id = {e["event_id"]: e for e in events}
    out = {}
    for cid in changed_ids:
        trip = by_id[cid]["trip_id"]
        first = min((e for e in events if e["trip_id"] == trip and e["event_type"] == "trip_completed"),
                    key=lambda e: (e["event_time"], e["event_id"]))
        out[trip] = (first["fare"], by_id[cid]["fare"])
    return out


@then("the fare of each of the {n:d} changed trips is {more:f} more than in its first trip_completed")
def truth_fare(context, n, more):
    faults = truth.by_kind(truth.read_faults(context.out / context.sid))
    fares = first_fares(context.out / context.sid / "_truth" / "events.jsonl", faults["changed"])
    assert len(fares) == n
    for trip, (first, _) in fares.items():
        assert round(context.truth["fare_by_trip"][trip] - first, 2) == more, trip


@when("line {line:d} of _truth/events.jsonl is validated against its model")
def validate(context, line):
    text = (context.data / context.sid / "_truth" / "events.jsonl").read_text().splitlines()[line - 1]
    try:
        validate_line(text, EVENT, "events.jsonl", line)
        context.error = None
    except LineError as err:
        context.error = err


@then('the validation stops with an error naming "{file}", line {line:d} and the field "{field}"')
def names_field(context, file, line, field):
    err = context.error
    assert err is not None, "the line validated"
    assert (err.file, err.line, err.field) == (file, line, field), str(err)
    assert f"{file}, line {line}, field {field}" in str(err)


# --- the replayer --------------------------------------------------------------------------------------------------

@given("these deliveries:")
def these(context):
    context.deliveries = [{k: (int(v) if k == "pickup_zone_id" else v) for k, v in row.items()}
                          for row in context.table]


def _send(context, deliveries, speed, run, to, slot_min=1):
    context.evidence = context.tmp / "evidence" / run
    context.fake = FakeTime()
    s = sess()
    if to == "local":
        context.received = getattr(context, "received", [])
        context.sender = replay.LocalSender(context.received.extend)
    elif to == "kinesis":
        s.client("kinesis").create_stream(StreamName="recipe-trip-events", ShardCount=1)
        context.sender = replay.make_sender("kinesis", session=s)
    elif to == "s3":
        s.client("s3").create_bucket(Bucket="recipe-lake-123456789012",
                                     CreateBucketConfiguration={"LocationConstraint": REGION})
        context.sender = replay.make_sender("s3", run_id=run, slot_min=slot_min, session=s)
    context.deliveries = deliveries
    context.summary = replay.replay(deliveries, context.sender, run, speed, evidence=context.evidence,
                                    clock=context.fake.clock, wall=context.fake.wall, sleep=context.fake.sleep)


@when('the replayer sends them at speed {speed:g} with run id "{run}" to "{to}"')
def send_them(context, speed, run, to):
    _send(context, context.deliveries, speed, run, to)


@when('the replayer sends it at speed {speed:g} with run id "{run}" to "{to}" with slots of {slot:d} minute')
def send_slots(context, speed, run, to, slot):
    _send(context, replay.load_deliveries(context.sid, context.data), speed, run, to, slot)


@when('the replayer sends it at speed {speed:g} with run id "{run}" to "{to}"')
def send_it(context, speed, run, to):
    _send(context, replay.load_deliveries(context.sid, context.data), speed, run, to)


def _offsets(context):
    lines = jsonl(context.evidence / "sent.jsonl")
    first = replay.parse(lines[0]["sent_time"])
    return lines, [(replay.parse(x["sent_time"]) - first).total_seconds() for x in lines]


@then("sent.jsonl has the {n:d} deliveries, sent {a:d} s, {b:d} s and {c:d} s after the start, each within 1 s")
def sent_at(context, n, a, b, c):
    lines, offsets = _offsets(context)
    assert len(lines) == n and all(x["result"] == "ok" for x in lines)
    assert all(abs(o - want) <= 1 for o, want in zip(offsets, (a, b, c))), offsets


@then('the handler received trip ids "{a}", "{b}" and "{c}"')
def handler_got(context, a, b, c):
    assert [d["trip_id"] for d in context.received] == [a, b, c]


@then("{n:d} records reached the stream, in PutRecords calls of at most {limit:d}")
def reached_stream(context, n, limit):
    kin = sess().client("kinesis")
    shard = kin.list_shards(StreamName="recipe-trip-events")["Shards"][0]["ShardId"]
    it = kin.get_shard_iterator(StreamName="recipe-trip-events", ShardId=shard,
                                ShardIteratorType="TRIM_HORIZON")["ShardIterator"]
    records = []
    while it:
        page = kin.get_records(ShardIterator=it, Limit=10000)
        records += page["Records"]
        it = page.get("NextShardIterator") if page["Records"] else None
    assert len(records) == n and max(context.sender.calls) <= limit, (len(records), context.sender.calls)


@then("the last delivery was sent (its received_time minus the first received_time) / {speed:d} after the start, "
      "within 1 s")
def last_sent(context, speed):
    lines, offsets = _offsets(context)
    times = sorted(replay.parse(d["received_time"]) for d in context.deliveries)
    assert abs(offsets[-1] - (times[-1] - times[0]).total_seconds() / speed) <= 1, offsets[-1]


@then('each object under raw/trip-events/dt={day}/ is named by its slot start and "{run}"')
def named_by_slot(context, day, run):
    s3 = sess().client("s3")
    objs = [o["Key"] for page in s3.get_paginator("list_objects_v2").paginate(
        Bucket="recipe-lake-123456789012", Prefix="raw/trip-events/") for o in page.get("Contents", [])]
    context.keys = objs
    assert objs and all(k.startswith(f"raw/trip-events/dt={day}/") for k in objs), objs[:3]
    for key in objs:
        slot = key.rsplit("/", 1)[1].removesuffix(f"-{run}.jsonl")
        datetime.strptime(slot, "%Y%m%dT%H%MZ")


@then("the objects hold {n:d} lines together")
def lines_together(context, n):
    s3 = sess().client("s3")
    total = sum(len(s3.get_object(Bucket="recipe-lake-123456789012", Key=k)["Body"].read().splitlines())
                for k in context.keys)
    assert total == n


@when('it is replayed again with run id "{run}" to "{to}"')
def replayed_again(context, run, to):
    replay.replay(replay.load_deliveries(context.sid, context.data), replay.LocalSender(context.received.extend), run)


@then("the handler received {n:d} deliveries with {trips:d} distinct trip ids")
def distinct(context, n, trips):
    assert len(context.received) == n and len({d["trip_id"] for d in context.received}) == trips


# --- the whole run -------------------------------------------------------------------------------------------------

@when("the pipeline processes the run offline")
def offline_run(context):
    run = "offline"
    replay.replay(replay.load_deliveries(context.sid, context.data), replay.LocalSender(pipeline.handle), run)
    rows = pipeline.stored_events()
    for r in rows:
        r["trip_id"], r["event_id"] = replay.unprefixed(r["trip_id"], run), replay.unprefixed(r["event_id"], run)
    context.tables = {"requested_per_zone": {str(k): v for k, v in pipeline.requested_per_zone(rows).items()},
                      "fare_by_trip": pipeline.fare_by_trip(rows)}


@when('the pipeline processes the run in the deployed system with run id "{run}"')
def live_whole_run(context, run):
    import live_run
    to, speed = os.environ.get("LIVE_TARGET") or "s3", float(os.environ.get("LIVE_SPEED") or 0)
    context.run_id = run
    context.tables = live_run.run(sess(), context.sid, to, speed, run)


@then("the table of trips requested per zone holds {counts} trips for zones {zones}")
def per_zone(context, counts, zones):
    want = dict(zip(map(str, zones_of(zones)), zones_of(counts)))
    assert context.tables["requested_per_zone"] == want, context.tables["requested_per_zone"]


@then("the stored fare of each of the {n:d} changed trips is {more:f} more than in its first trip_completed")
def stored_fare(context, n, more):
    faults = truth.by_kind(truth.read_faults(context.data / context.sid))
    fares = first_fares(context.data / context.sid / "_truth" / "events.jsonl", faults["changed"])
    assert len(fares) == n
    for trip, (first, _) in fares.items():
        assert round(context.tables["fare_by_trip"][trip] - first, 2) == more, trip


@then("the run's output is in evidence/{run}/")
def evidence_kept(context, run):
    run = run.rstrip("/")
    folder_ = ROOT / "evidence" / run
    for name in ("sent.jsonl", "command.txt", "tables.json", "summary.json"):
        assert (folder_ / name).is_file(), name
    assert json.loads((folder_ / "tables.json").read_text())["matches_truth"] is True


# --- the scheduled check -------------------------------------------------------------------------------------------

def vancouver(day: str, hhmm: str, tz="America/Vancouver") -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime(*map(int, day.split("-")), h, m, tzinfo=ZoneInfo(tz)).astimezone(timezone.utc)


def _deployed(context):
    """The stand-in deployed system (rides.pipeline in moto), the run log, the topic and a queue that keeps what
    the topic publishes; the function's settings as the environment of this process."""
    if getattr(context, "check_ready", False):
        return
    s = sess()
    os.environ.setdefault("CHECK_TARGET", "local:rides.pipeline:handle")
    os.environ.setdefault("CHECK_QUERY", "local:")
    os.environ["RUNS_TABLE"] = ensure_run_log(s)
    sns, sqs = s.client("sns"), s.client("sqs")
    topic = sns.create_topic(Name="recipe-check-alerts")["TopicArn"]
    queue = sqs.create_queue(QueueName="recipe-alerts-seen")["QueueUrl"]
    qarn = sqs.get_queue_attributes(QueueUrl=queue, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
    sns.subscribe(TopicArn=topic, Protocol="sqs", Endpoint=qarn, Attributes={"RawMessageDelivery": "true"})
    os.environ["TOPIC_ARN"] = topic
    context.queue = queue
    context.metric = []
    context.session = s
    s.events.register("before-parameter-build.cloudwatch.PutMetricData",
                      lambda params, **_: context.metric.extend(params["MetricData"]))
    context.check_ready = True
    pipeline._table()                      # the deployed system exists before the first send


def _call(context, phase, now, day=None):
    event = {"phase": phase, **({"date": day} if day else {})}
    return live_check.handler(event, now=now, sess=context.session)


@given("the deployed system stores what it is sent")
def stores(context):
    _deployed(context)


@given("the send schedule delivers its call twice")
def twice(context):
    for _ in range(2):
        context.sent = _call(context, "send", vancouver("2026-11-24", "06:00"))


@when("the send schedule fires at {send} and the read schedule at {read}")
def send_and_read(context, send, read):
    context.sent = _call(context, "send", vancouver("2026-11-24", send))
    context.item = _call(context, "read", vancouver("2026-11-24", read))


@when("the read schedule fires at {read}")
def read_fires(context, read):
    context.item = _call(context, "read", vancouver("2026-11-24", read))


@then('the check sent the simulated data set SIM03 "probe" with {n:d} events')
def sent_probe(context, n):
    probe = {e["event_id"] for e in pipeline.stored_events() if e["event_id"].startswith("probe-2026-11-24-")}
    assert context.sent["sent"] == n and len(probe) == n


@then("it read {n:d} probe trips of the day and put {v:d} to the metric LiveCheck/CheckPassed")
def read_n(context, n, v):
    assert context.item["checked_count"] == n and context.item["run_state"] == "finished", context.item
    assert context.item["run_id"] == "2026-11-24"
    assert context.metric[-1]["MetricName"] == "CheckPassed" and context.metric[-1]["Value"] == v


@given("CHECK_CRON is {hhmm} every day and CHECK_DELAY_MIN is {delay:d}")
def cron_daily(context, hhmm, delay):
    h, m = map(int, hhmm.split(":"))
    os.environ["CHECK_CRON"] = f"cron({m} {h} * * ? *)"
    os.environ["CHECK_DELAY_MIN"] = str(delay)


@given('CHECK_CRON is "{expr}"')
def cron_expr(context, expr):
    os.environ["CHECK_CRON"] = expr


@when("scripts/check.py up creates the schedules")
def check_up(context):
    os.environ.setdefault("CHECK_TARGET", "kinesis:recipe-trip-events")
    os.environ.setdefault("CHECK_QUERY", "dynamodb:recipe-sim-events")
    try:
        context.up = check.up(sess(), log=lambda *_: None, sleep=lambda s: None)
        context.error = None
    except ValueError as err:
        context.error = err


@then("the read schedule fires at {hhmm} every day")
def read_at(context, hhmm):
    h, m = map(int, hhmm.split(":"))
    got = sess().client("scheduler").get_schedule(Name="recipe-check-read", GroupName="recipe-simulated-runs")
    assert got["ScheduleExpression"] == f"cron({m} {h} * * ? *)", got["ScheduleExpression"]
    assert got["ScheduleExpressionTimezone"] == "America/Vancouver"


@then('it stops with an error that holds "{text}" and creates nothing')
def stops(context, text):
    assert context.error is not None and text in str(context.error)
    s = sess()
    assert not s.client("lambda").list_functions()["Functions"]
    assert not s.client("sns").list_topics()["Topics"]
    assert not s.client("dynamodb").list_tables()["TableNames"]


@given("the check of {day} read {n:d} probe trips")
def check_read_n(context, day, n):
    _deployed(context)
    _call(context, "send", vancouver(day, "06:00"))
    if n < 20:
        table = boto3.resource("dynamodb", region_name=REGION).Table(pipeline.table_name())
        trip = sorted({e["trip_id"] for e in pipeline.stored_events() if e["trip_id"].startswith(f"probe-{day}-")})[0]
        for e in pipeline.stored_events():
            if e["trip_id"] == trip:
                table.delete_item(Key={"event_id": e["event_id"]})
    context.item = _call(context, "read", vancouver(day, "07:30"))


@given("the check of {day} failed to read the table")
def check_failed(context, day):
    _deployed(context)
    _call(context, "send", vancouver(day, "06:00"))
    os.environ["CHECK_QUERY"] = "dynamodb:recipe-no-such-table"
    context.item = _call(context, "read", vancouver(day, "07:30"))
    assert context.item["error_text"], context.item


@given("the check of {day} did not run")
def check_none(context, day):
    _deployed(context)


def _published(context) -> list[str]:
    sqs = context.session.client("sqs")
    msgs = sqs.receive_message(QueueUrl=context.queue, MaxNumberOfMessages=10, WaitTimeSeconds=0).get("Messages", [])
    context.messages = getattr(context, "messages", []) + [m["Body"] for m in msgs]
    return context.messages


@when("the verdict schedule fires at {day}T{hhmm} Vancouver time")
def verdict_at(context, day, hhmm):
    _deployed(context)
    context.verdicts = [_call(context, "verdict", vancouver(day, hhmm))]


@when("the verdict schedule fires at 00:15 on each of the {n:d} days from {first} to {last}")
def verdict_week(context, n, first, last):
    _deployed(context)
    d0, d1 = date.fromisoformat(first), date.fromisoformat(last)
    days = [d0 + timedelta(days=i) for i in range((d1 - d0).days + 1)]
    assert len(days) == n
    context.verdicts = [_call(context, "verdict", vancouver(d.isoformat(), "00:15")) for d in days]


@then("nothing is published to recipe-check-alerts")
def nothing(context):
    assert _published(context) == [] and all(v["published"] is None for v in context.verdicts), context.verdicts


@then('recipe-check-alerts gets "{text}" and the read\'s error_text')
def gets_error(context, text):
    msgs = _published(context)
    assert msgs == [f"{text}{context.item['error_text']}"], msgs


@then('recipe-check-alerts gets "{text}"')
def gets(context, text):
    assert _published(context) == [text], context.messages


@then("the verdict judged {judged:d} day and passed over {passed:d}")
def judged(context, judged, passed):
    due = [v for v in context.verdicts if v["due"]]
    assert (len(due), len(context.verdicts) - len(due)) == (judged, passed), context.verdicts


@given("CHECK_TZ is {zone}")
def check_tz(context, zone):
    os.environ["CHECK_TZ"] = zone


@when("the read phase runs at {when_}")
def read_at_utc(context, when_):
    _deployed(context)
    context.item = _call(context, "read", replay.parse(when_))


@then('its item in the run log has run_id "{run_id}"')
def item_run_id(context, run_id):
    got = context.session.client("dynamodb").get_item(
        TableName=os.environ["RUNS_TABLE"], Key={"job": {"S": "live-check"}, "run_id": {"S": run_id}})
    assert "Item" in got and context.item["run_id"] == run_id, context.item


@when("scripts/check.py up creates the function's role")
def up_role(context):
    os.environ["CHECK_TARGET"] = "kinesis:recipe-trip-events"
    os.environ["CHECK_QUERY"] = "dynamodb:recipe-sim-events"
    check.up(sess(), log=lambda *_: None, sleep=lambda s: None)
    doc = sess().client("iam").get_role_policy(RoleName="recipe-check-role", PolicyName="check")["PolicyDocument"]
    context.policy = json.loads(doc) if isinstance(doc, str) else doc


def _allowed(policy, action):
    return [s["Resource"] for s in policy["Statement"] if action in s["Action"]]


@then('its policy allows "{a}" and "{b}" on the table {table} only')
def allows_table(context, a, b, table):
    arn = f"arn:aws:dynamodb:{REGION}:123456789012:table/{table}"
    assert _allowed(context.policy, a) == [arn] and _allowed(context.policy, b) == [arn]


@then('its policy allows "{a}" on the topic {topic} only')
def allows_topic(context, a, topic):
    assert _allowed(context.policy, a) == [f"arn:aws:sns:{REGION}:123456789012:{topic}"]


@when("scripts/check.py alarm creates the alarm {name}")
def make_alarm(context, name):
    check.alarm(sess(), log=lambda *_: None)
    context.alarm = sess().client("cloudwatch").describe_alarms(AlarmNames=[name])["MetricAlarms"][0]


@then("its metric is {metric} with the statistic {stat} and a period of {period:d} s")
def alarm_metric(context, metric, stat, period):
    a = context.alarm
    assert f"{a['Namespace']}/{a['MetricName']}" == metric and a["Statistic"] == stat and a["Period"] == period


@then("it needs {m:d} of {n:d} periods below {threshold:d}, with missing data treated as breaching")
def alarm_periods(context, m, n, threshold):
    a = context.alarm
    assert (a["DatapointsToAlarm"], a["EvaluationPeriods"], a["Threshold"]) == (m, n, threshold)
    assert a["ComparisonOperator"] == "LessThanThreshold" and a["TreatMissingData"] == "breaching"


@then("its action is the topic {topic}")
def alarm_action(context, topic):
    assert [x.rsplit(":", 1)[-1] for x in context.alarm["AlarmActions"]] == [topic]


# --- the scheduled check on AWS (@live) ----------------------------------------------------------------------------

@given("the check is deployed with the lake bucket as its target and its query")
def live_deployed(context):
    s = sess()
    bucket = f"{os.environ.get('PREFIX') or 'recipe'}-lake-{s.client('sts').get_caller_identity()['Account']}"
    os.environ["CHECK_TARGET"] = f"s3:{bucket}"
    os.environ["CHECK_QUERY"] = f"s3:{bucket}"
    context.up = check.up(s)


@when("the deployed function sends the probe and then reads it")
def live_send_read(context):
    today = live_check.local_day(datetime.now(timezone.utc))
    context.today = today.isoformat()
    context.live_sent = check.invoke("send")
    context.item = check.invoke("read", context.today)
    folder_ = ROOT / "evidence" / f"live-check-{context.today}"
    folder_.mkdir(parents=True, exist_ok=True)
    (folder_ / "send.json").write_text(json.dumps(context.live_sent, indent=1, sort_keys=True) + "\n")
    (folder_ / "read.json").write_text(json.dumps(context.item, indent=1, sort_keys=True) + "\n")


@then("the day's item in the run log is finished with {n:d} probe trips")
def live_item(context, n):
    assert context.live_sent["sent"] == 60, context.live_sent
    assert context.item["run_state"] == "finished" and context.item["checked_count"] == n, context.item


@then('the deployed verdict of {day}, a day without an item, publishes "{text}" to the check\'s topic')
def live_verdict(context, day, text):
    got = check.invoke("verdict", day.split(",")[0])
    folder_ = ROOT / "evidence" / f"live-check-{context.today}"
    (folder_ / "verdict.json").write_text(json.dumps(got, indent=1, sort_keys=True) + "\n")
    assert got["due"] and got["message"] == text and got["published"], got


# --- CI and the default run ----------------------------------------------------------------------------------------

@when(".github/workflows/offline.yml is read")
def read_workflow(context):
    context.workflow = (ROOT / ".github" / "workflows" / "offline.yml").read_text()


@then('it runs "{a}" and "{b}" on every push')
def runs_on_push(context, a, b):
    w = context.workflow
    assert a in w and b in w and "push:" in w, w


@then("it does not hold an AWS secret or a role to assume")
def no_secret(context):
    w = context.workflow.lower()
    for word in ("secrets.", "aws-access-key", "role-to-assume", "configure-aws-credentials", "id-token"):
        assert word not in w, word


@when("behave.ini is read")
def read_ini(context):
    context.ini = configparser.ConfigParser()
    context.ini.read(ROOT / "behave.ini")


@then('default_tags is "{value}"')
def default_tags(context, value):
    assert context.ini["behave"]["default_tags"] == value


# --- clean-up ------------------------------------------------------------------------------------------------------

@given("the lake bucket, the shared workgroup, the stream, the check and its alarm were made")
def everything_made(context):
    import lake
    import live_run
    s = sess()
    os.environ["CHECK_TARGET"] = "kinesis:recipe-trip-events"
    os.environ["CHECK_QUERY"] = "dynamodb:recipe-sim-events"
    lake.up(s, log=lambda *_: None)
    lake.ensure_workgroup("recipe-wg", sess=s)
    live_run.stream_up(s, log=lambda *_: None)
    check.up(s, log=lambda *_: None, sleep=lambda x: None)
    check.alarm(s, log=lambda *_: None)


@when("clean_up.py runs without --yes")
def clean_dry(context):
    import clean_up
    context.lines = []
    context.steps = clean_up.clean(sess(), yes=False, log=context.lines.append)


@then("it lists what it would remove and deletes nothing")
def lists_only(context):
    assert not context.steps.failed and any(x.startswith("would remove") for x in context.lines)
    assert sess().client("lambda").list_functions()["Functions"]


@when("clean_up.py --yes runs and then scripts/lake.py down --yes")
def clean_yes_down(context):
    import clean_up
    import lake
    assert not clean_up.clean(sess(), yes=True, log=lambda *_: None).failed
    assert lake.down(sess(), yes=True, log=lambda *_: None) == 0


@then("clean_up.py --check finds nothing left by schedule group or by name prefix")
def nothing_left(context):
    import clean_up
    out = []
    assert clean_up.check(sess(), log=out.append) == 0, out


@given("a run log {name} that another recipe made")
def borrowed_log(context, name):
    sess().client("dynamodb").create_table(
        TableName=name, BillingMode="PAY_PER_REQUEST",
        KeySchema=[{"AttributeName": "job", "KeyType": "HASH"}, {"AttributeName": "run_id", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "job", "AttributeType": "S"},
                              {"AttributeName": "run_id", "AttributeType": "S"}])
    ensure_run_log(sess())


@when("clean_up.py --yes runs")
def clean_yes(context):
    import clean_up
    assert not clean_up.clean(sess(), yes=True, log=lambda *_: None).failed


@then("the table {name} is still there")
def still_there(context, name):
    assert name in sess().client("dynamodb").list_tables()["TableNames"]


@given('the lake bucket holds a slot file of this recipe\'s replay "{run_id}" and one of another recipe\'s replay')
def own_and_foreign_slot_files(context, run_id):
    import clean_up
    import lake
    import tags
    s = sess()
    saved = clean_up.EVIDENCE
    clean_up.EVIDENCE = context.tmp / "evidence"            # no evidence/ folders: the ledger row is the record
    context.add_cleanup(setattr, clean_up, "EVIDENCE", saved)
    lake.up(s, log=lambda *_: None)
    context.bucket = lake.bucket_name(s)
    tags.record("s3-replay", run_id, "created", script="scripts/live_run.py run")
    day = "raw/trip-events/dt=2026-11-23/20261123T0900Z"
    context.foreign = f"{day}-another-recipe-run.jsonl"
    for key in (f"{day}-{run_id}.jsonl", context.foreign):
        s.client("s3").put_object(Bucket=context.bucket, Key=key, Body=b"{}\n")


@then("only the other recipe's slot file is left under raw/trip-events/")
def only_foreign_left(context):
    listed = sess().client("s3").list_objects_v2(Bucket=context.bucket, Prefix="raw/trip-events/")
    assert [o["Key"] for o in listed.get("Contents", [])] == [context.foreign]

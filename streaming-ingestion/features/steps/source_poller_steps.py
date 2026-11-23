"""Steps of features/source_poller.feature (ext-source-poller). The scenarios run in moto with PREFIX=recipe.

The partner's feed is the prefix feed/ of the lake bucket in moto: the slot files and a manifest that, before each
run, lists the files published by the run's time (as the partner's would). The poller is ext-source-poller/poller.py's
run(), called with the scenario's clock; the main consumer (main/consumer.py) reads the stream through
main/local_poller.py when a Then needs the table.
"""
from __future__ import annotations

import base64
import contextlib
import io
import json
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from behave import given, then, use_step_matcher, when

import support

SLOT = timedelta(minutes=15)


def _t(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def _stamp(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _setup(context):
    if getattr(context, "sp", None):
        return context.sp
    import boto3
    import feed
    import lake
    import poller
    import run_log
    import slots

    run = support.load("source_poller_run", "ext-source-poller/run.py")
    sess = boto3.Session(region_name="ca-central-1")
    with support.own_calls():
        run.make_local(sess, "recipe")
        lake.up(sess)
    bucket = lake.bucket_name(sess)
    n = run.infra.names("recipe")
    context.sp = SimpleNamespace(run=run, feed=feed, poller=poller, slots=slots, sess=sess, bucket=bucket, n=n,
                                 ddb=sess.client("dynamodb"), s3=sess.client("s3"), kinesis=sess.client("kinesis"),
                                 run_log=run_log.run_log_name(), entries=[], files={}, until=None, lines=[],
                                 runs=[], last_lines=[])
    return context.sp


def _put(context, name: str, body: bytes) -> None:
    sp = context.sp
    with support.own_calls():
        sp.s3.put_object(Bucket=sp.bucket, Key=f"feed/{name}", Body=body)


def _trip_line(slot: datetime, i: int, tag: str) -> str:
    return json.dumps({"event_type": "trip_completed", "event_id": f"{tag}-{slot:%H%M}-{i:04d}",
                       "trip_id": f"t-{tag}-{slot:%H%M}-{i:04d}", "driver_id": f"d-{i % 40:04d}",
                       "dropoff_zone_id": 48, "trip_miles": 1.2, "trip_time_s": 420, "fare": 11.5,
                       "driver_pay": 8.3, "event_time": _stamp(slot + timedelta(seconds=i % 899))}, sort_keys=True)


def _add_slot(context, slot: str, file: str, published: str | None, trips: int = 3, body: bytes | None = None):
    sp = _setup(context)
    body = body or ("\n".join(_trip_line(_t(slot), i, "x") for i in range(1, trips + 1)) + "\n").encode()
    sp.entries.append({"slot": slot, "file": file, "published_time": published, "trips": trips})
    sp.files[file] = body
    _put(context, file, body)


def _manifest_at(context, at: datetime) -> None:
    sp = context.sp
    until = min(at, sp.until) if sp.until else at
    listed = [{k: e[k] for k in ("slot", "file", "published_time") if e.get(k)} for e in sp.entries
              if not e.get("never") and (not e.get("published_time") or _t(e["published_time"]) <= until)]
    _put(context, "manifest.json", json.dumps({"slots": listed}).encode())


def _run(context, at: datetime, stop_after_claim=False):
    sp = _setup(context)
    _manifest_at(context, at)
    f = sp.poller.Feed(f"s3://{sp.bucket}/feed/", s3=sp.s3)
    out = io.StringIO()
    context.calls.clear_made()
    with contextlib.redirect_stdout(out):
        try:
            sp.runs.append(sp.poller.run(at, ddb=sp.ddb, kinesis=sp.kinesis, feed=f, slots_table=sp.n["slots_table"],
                                         run_log=sp.run_log, stream=sp.n["stream"], sleep=lambda s: None,
                                         wall=lambda: at + timedelta(seconds=5), stop_after_claim=stop_after_claim))
        except SystemExit:
            pass                                   # the stand-in for a run that stops after its claim
    lines = [json.loads(x) for x in out.getvalue().splitlines() if x.startswith("{")]
    sp.last_lines = [x for x in lines if "slot" in x]
    sp.lines += sp.last_lines
    sp.last_queries = [p for p in context.calls.of("Query") if p["TableName"] == sp.n["slots_table"]]
    context.printed += out.getvalue()


def _item(context, slot: str) -> dict | None:
    sp = context.sp
    got = sp.ddb.get_item(TableName=sp.n["slots_table"], Key={"slot_day": {"S": slot[:10]},
                                                              "slot_start": {"S": slot}}, ConsistentRead=True)
    return {k: next(iter(v.values())) for k, v in got["Item"].items()} if "Item" in got else None


# ---------- the feed

@given("the manifest lists these slots:")
def step_manifest(context):
    for row in context.table:
        _add_slot(context, row["slot"], row["file"], row["published_time"] or None)


@given("the manifest lists slot {slot}")
def step_manifest_one(context, slot):
    end = _t(slot) + SLOT
    _add_slot(context, slot, f"trips-{_t(slot):%Y-%m-%dT%H%M}Z.jsonl", _stamp(end + timedelta(minutes=2)))


@given("slot {slot} has a file of {n:d} trips")
def step_slot_file(context, slot, n):
    context.the_slot = slot
    _add_slot(context, slot, f"trips-{_t(slot):%Y-%m-%dT%H%M}Z.jsonl", _stamp(_t(slot) + SLOT + timedelta(minutes=2)),
              trips=n)


SUBSETS = {"feed-day": None, "overlapping-runs": ["2026-11-22T08:00Z", "2026-11-22T08:15Z"],
           "stopped-run": ["2026-11-22T08:00Z"], "unreadable-file": ["2026-11-22T08:00Z"]}

use_step_matcher("re")


@given(r'the simulated data set (?P<sid>SIM\d+) "(?P<name>[^"]+)" has been published(?: up to (?P<until>\S+Z))?')
def step_published(context, sid, name, until=None):
    sp = _setup(context)
    wanted = SUBSETS[name]
    for s in sp.feed.day_slots(context.tmp / "data"):
        if wanted is not None and s["slot"] not in wanted:
            continue
        sp.entries.append({**{k: s[k] for k in ("slot", "file", "published_time", "trips")},
                           "never": s["published_time"] is None})      # the data set's slot that is never published
        sp.files[s["file"]] = s["body"]
        _put(context, s["file"], s["body"])
    sp.until = _t(until) if until else None
    if name == "unreadable-file":
        key = f"feed/{sp.entries[0]['file']}"

        def cut(answer, params):
            whole = answer["Body"].read()
            return {**answer, "Body": io.BytesIO(whole[: len(whole) // 2 + 7])}   # stops inside a line
        context.calls.change("GetObject", cut, times=1, when=lambda p: p.get("Key") == key)


@given(r"slot (?P<slot>\S+Z) is loaded(?:, and its item is under slot_day (?P<day>[\d-]+))?")
def step_slot_loaded(context, slot, day=None):
    sp = _setup(context)
    with support.own_calls():
        sp.ddb.put_item(TableName=sp.n["slots_table"], Item={
            "slot_day": {"S": day or slot[:10]}, "slot_start": {"S": slot}, "slot_state": {"S": "loaded"},
            "record_total": {"N": "3"}, "claimed_at": {"S": _stamp(_t(slot) + SLOT)}})


@given(r"the poller has run every 15 minutes (?:from (?P<a>\S+Z) )?(?:to|up to) (?P<b>\S+Z)")
@when(r"the poller has run every 15 minutes (?:from (?P<a>\S+Z) )?(?:to|up to) (?P<b>\S+Z)")
def step_every_15(context, b, a=None):
    if "live" in context.scenario.effective_tags:
        return                                     # in AWS the schedule runs the poller; the Then reads its log
    sp = _setup(context)
    t, end = _t(a or "2026-11-22T00:15:00Z"), _t(b)
    while t <= end:
        _run(context, t)
        t += SLOT
    context.all_lines = sp.lines


@then(r"slot (?P<slot>\S+Z) is (?P<what>loaded|still loading|failed)(?P<rest>.*)")
def step_slot_is(context, slot, what, rest):
    item = _item(context, slot)
    assert item, f"no item for {slot}"
    if what == "still loading":
        assert item["slot_state"] == "loading", item
        return
    if what == "failed":
        assert item["slot_state"] == "failed", item
        needle = rest.split('"')[1]
        assert needle in item["error_text"], item
        return
    rest = rest.strip()
    if rest == "and marked late":
        assert item["slot_state"] == "late", item
    elif rest.startswith("with first_seen_time"):
        seen = rest.split()[2]
        assert item["slot_state"] == "loaded" and item["first_seen_time"] == seen, item
    elif rest.startswith("with"):
        assert item["slot_state"] == "loaded" and int(item["record_total"]) == int(rest.split()[1]), item
    else:
        assert item["slot_state"] == "loaded", item


@then(r"GET /api/slots\?day=(?P<day>\S+) gets (?P<code>\d+)")
def step_get_code(context, day, code):
    sp = context.sp
    status, _ = sp.slots.get_slots({"day": day}, sp.ddb, sp.n["slots_table"], context.page_at)
    assert status == int(code), status


use_step_matcher("parse")


@when("the poller runs at {at}")
def step_poller_runs(context, at):
    _run(context, _t(at))


@given("the poller ran at {a} and at {b}")
def step_ran_twice(context, a, b):
    _run(context, _t(a))
    _run(context, _t(b))


@when("the poller loads the slot")
def step_loads_the_slot(context):
    _run(context, _t(context.the_slot) + SLOT + timedelta(minutes=15))


@then("the poller loaded slot {slot} only")
def step_loaded_only(context, slot):
    got = [x["slot"] for x in context.sp.last_lines if x["slot_state"] in ("loaded", "late")]
    assert got == [slot], got


@then("the poller loaded no slot")
def step_loaded_none(context):
    assert [x for x in context.sp.last_lines if x["slot_state"] in ("loaded", "late")] == [], context.sp.last_lines


@then("slot {slot} does not have an item")
def step_no_item(context, slot):
    assert _item(context, slot) is None


@then("the poller queried slot_day {a}, {b} and {c}")
def step_queried(context, a, b, c):
    days = [q["ExpressionAttributeValues"][":d"]["S"] for q in context.sp.last_queries]
    assert days == [a, b, c], days


@then("{n:d} slots are loaded, {late:d} of them late")
def step_n_loaded(context, n, late):
    states = [x["slot_state"] for x in context.sp.lines]
    published = [e for e in context.sp.entries if e["published_time"]]
    assert n == len(published)                              # the data set's number, known before the run
    assert states.count("loaded") + states.count("late") == n and states.count("late") == late, states


def _stream_records(context) -> list[dict]:
    sp = context.sp
    out = []
    with support.own_calls():
        for shard in sp.kinesis.list_shards(StreamName=sp.n["stream"])["Shards"]:
            it = sp.kinesis.get_shard_iterator(StreamName=sp.n["stream"], ShardId=shard["ShardId"],
                                               ShardIteratorType="TRIM_HORIZON")["ShardIterator"]
            while it:
                page = sp.kinesis.get_records(ShardIterator=it, Limit=10000)
                out += [json.loads(r["Data"]) for r in page["Records"]]
                if not page["Records"]:
                    break
                it = page.get("NextShardIterator")
    return out


@then("table recipe-updates holds each trip of the {n:d} published files once")
def step_table_each_once(context, n):
    import consumer
    import local_poller

    sp = context.sp
    with contextlib.redirect_stdout(io.StringIO()), support.own_calls():
        local_poller.drain(sp.kinesis, sp.n["stream"],
                           lambda e: consumer.handler(e, ddb=sp.ddb, table=sp.n["table"]), batch_size=1000)
    published = [e for e in sp.entries if e["published_time"]]
    assert len(published) == n
    expected = {json.loads(x)["event_id"] for e in published for x in sp.files[e["file"]].decode().splitlines()}
    with support.own_calls():
        stored = [i["update_id"] for i in support.items(sp.n["table"])]
    assert len(stored) == len(set(stored)) == len(expected) and set(stored) == expected, (len(stored), len(expected))


# ---------- two runs, a stopped run

@when("two poller runs start at {at}")
def step_two_runs(context, at):
    sp = _setup(context)
    t = _t(at)
    _manifest_at(context, t)
    barrier, outs = threading.Barrier(2), [io.StringIO(), io.StringIO()]

    def one():
        f = sp.poller.Feed(f"s3://{sp.bucket}/feed/", s3=sp.s3)
        barrier.wait()
        sp.runs.append(sp.poller.run(t, ddb=sp.ddb, kinesis=sp.kinesis, feed=f, slots_table=sp.n["slots_table"],
                                     run_log=sp.run_log, stream=sp.n["stream"], sleep=lambda s: None,
                                     wall=lambda: t + timedelta(seconds=5)))
    # one redirect around both threads: redirect_stdout swaps the process's stdout, so two would undo each other
    threads = [threading.Thread(target=one) for _ in (0, 1)]
    with contextlib.redirect_stdout(outs[0]):
        for th in threads:
            th.start()
        for th in threads:
            th.join()
    text = outs[0].getvalue()
    sp.lines = [json.loads(x) for x in text.splitlines() if x.startswith("{") and '"slot"' in x]


@then("each of slots {a} and {b} was sent by one run only")
def step_sent_once(context, a, b):
    sp = context.sp
    sent = [x["slot"] for x in sp.lines if x["slot_state"] in ("loaded", "late")]
    assert sorted(sent) == sorted([a, b]), sent
    states = sorted(r["run_state"] for r in sp.runs)
    assert len(sp.runs) == 2, sp.runs
    assert states in (["finished", "skipped"], ["finished", "finished"]), states


@then("the stream received each trip of the two files once")
def step_stream_once(context):
    sp = context.sp
    ids = [r["update_id"] for r in _stream_records(context)]
    expected = {json.loads(x)["event_id"] for e in sp.entries for x in sp.files[e["file"]].decode().splitlines()}
    assert len(ids) == len(set(ids)) == len(expected) and set(ids) == expected, (len(ids), len(expected))


@given("the run at {at} claimed slot {slot} and is still sending")
def step_claimed_sending(context, at, slot):
    _claimed(context, at, slot)


@given("the run at {at} claimed slot {slot} and stopped")
def step_claimed_stopped(context, at, slot):
    _claimed(context, at, slot)


def _claimed(context, at, slot):
    sp = _setup(context)
    if not any(e["slot"] == slot for e in sp.entries):
        step_published(context, "SIM03", "stopped-run")
    _run(context, _t(at), stop_after_claim=True)
    assert _item(context, slot)["slot_state"] == "loading"


# ---------- the page

@when("the page lists day {day} at {at}")
def step_page(context, day, at):
    sp = _setup(context)
    context.page_at = _t(at)
    status, context.page = sp.slots.get_slots({"day": day}, sp.ddb, sp.n["slots_table"], context.page_at)
    assert status == 200, context.page
    context.shown = {s["slot_start"]: s for s in context.page["slots"]}


@then("slot {slot} is shown as {state}")
def step_shown(context, slot, state):
    assert context.shown[slot]["slot_state"] == state, context.shown[slot]


@then("the response has {n:d} slots in slot order, each with a slot_state")
def step_response(context, n):
    body = context.page
    starts = [s["slot_start"] for s in body["slots"]]
    assert len(starts) == n and starts == sorted(starts) and body["day"] == "2026-11-22", starts[:3]
    assert body["read_time"] == _stamp(context.page_at)
    for s in body["slots"]:
        assert s["slot_state"] in ("loaded", "late", "loading", "failed", "missing", "not due"), s
        assert ("record_total" in s) == (s["slot_state"] in ("loaded", "late")), s


@then("it shows {n:d} slots loaded, {late:d} of them late, and {missing:d} missing, slot {slot}")
def step_shows(context, n, late, missing, slot):
    states = [s["slot_state"] for s in context.page["slots"]]
    assert states.count("loaded") + states.count("late") == n and states.count("late") == late, states
    assert [s["slot_start"] for s in context.page["slots"] if s["slot_state"] == "missing"] == [slot] * missing


# ---------- @live (LIVE=1): after run.py --target aws and feed.py --to aws --live

@given('the simulated data set SIM01 "feed-day" has been published in AWS')
def step_live_published(context):
    import os
    from pathlib import Path

    context.published_file = Path(os.environ["LIVE_PUBLISHED"])       # evidence/feed-<start>/published.jsonl


@then("latency.py prints {n:d} latencies and a p95 under {m:d} minutes")
def step_live_latency(context, n, m):
    latency = support.load("source_poller_latency", "ext-source-poller/latency.py")
    support.run(context, latency.main, ["--published", str(context.published_file)])
    assert f"{n} latencies" in context.printed and ": met" in context.printed, context.printed[-500:]

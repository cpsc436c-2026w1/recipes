"""Steps of features/queue_ingest.feature (ext-queue-ingest). The scenarios run in moto with PREFIX=recipe.

The queue, its dead-letter queue, the table and the mapping are made by ext-queue-ingest/run.py's create(), as in
AWS. moto's own hand-over of a sent message to a mapped function is switched off; run.py's drain() plays Lambda's
poller (batches of up to 100, a failed message visible again at once in place of its visibility timeout), and calls
queue_consumer.handler with the scenario's boto3 clients. moto moves a message received 5 times to the dead-letter
queue at its next receive, as SQS does. The message move task is a stub (moto does not have it): it moves the
dead-letter queue's messages to the source queue and reports the task finished.
"""
from __future__ import annotations

import io
import json
import os
import time
from contextlib import redirect_stdout
from types import SimpleNamespace

from behave import given, then, use_step_matcher, when

import support

RUN_ID = "sim"


def _mods():
    import queue_consumer
    import replay
    import replay_dlq
    import stand_in

    run = support.load("queue_ingest_run", "ext-queue-ingest/run.py")
    return SimpleNamespace(run=run, consumer=queue_consumer, replay=replay, dlq=replay_dlq, stand_in=stand_in)


def _session():
    import boto3

    return boto3.Session(region_name="ca-central-1")


def _setup(context, sink="dynamodb"):
    """Create the extension's resources once per scenario, as run.py does."""
    if getattr(context, "qi", None):
        return context.qi
    m = _mods()
    os.environ["SINK"] = sink
    sess = _session()
    bucket = None
    if sink == "s3":
        import lake

        lake.up(sess)
        bucket = lake.bucket_name(sess)
        os.environ["BUCKET"] = bucket
    made = m.run.create(sess, os.environ["PREFIX"], sink, m.run.batch_window(), bucket)
    m.run._no_moto_poller()
    n = m.run.infra.names(os.environ["PREFIX"])
    os.environ["TABLE"] = n["trips_table"]
    context.qi = SimpleNamespace(m=m, made=made, n=n, sqs=sess.client("sqs"), ddb=sess.client("dynamodb"),
                                 s3=sess.client("s3"), bucket=bucket, counts={}, sent_bodies=[], results=[])
    return context.qi


def _url(context, name: str) -> str:
    return context.qi.sqs.get_queue_url(QueueName=name)["QueueUrl"]


def _rows(context, at: str | None):
    """The scenario's table rows as delivery lines."""
    out = []
    for row in context.table:
        d = {"event_type": "trip_completed", "event_id": row["event_id"], "trip_id": row["trip_id"],
             "driver_id": row["driver_id"], "dropoff_zone_id": int(row["dropoff_zone_id"]),
             "trip_miles": float(row["trip_miles"]), "trip_time_s": int(row["trip_time_s"]),
             "fare": float(row["fare"]), "driver_pay": float(row["driver_pay"]), "event_time": row["event_time"],
             "delivery_id": f"dl-{row['event_id']}", "received_time": at or row["event_time"]}
        out.append(d)
    return out


def _handle(context, event, ctx=None):
    q = context.qi
    out = io.StringIO()
    with redirect_stdout(out):
        result = q.m.consumer.handler(event, ctx, ddb=q.ddb, s3=q.s3, clock=getattr(context, "clock", None) or time.time)
    context.printed += out.getvalue()
    q.results.append(result)
    for line in out.getvalue().splitlines():
        row = json.loads(line)
        if "batch" in row:
            for k in ("stored", "repeat", "other", "failed", "objects"):
                q.counts[k] = q.counts.get(k, 0) + row.get(k, 0)
    return result


def _drain(context, rounds=100, visible_again=True):
    q = context.qi
    calls = []

    def handler(event, ctx=None):
        calls.append(len(event["Records"]))
        # the drain prints nothing of its own; the consumer's lines are counted in _handle
        return _handle(context, event)
    q.m.run.drain(q.sqs, q.made["queue_url"], lambda e, c=None: handler(e), rounds=rounds, visible_again=visible_again)
    return calls


def _items(context):
    return support.items(context.qi.n["trips_table"])


def _trip(value: str) -> str:
    return value.removeprefix(f"{RUN_ID}-")


# ---------- sending

use_step_matcher("re")


@given(r"these messages are sent to queue (?P<queue>\S+?)(?: at (?P<at>\S+Z))?:")
def step_sent(context, queue, at=None):
    q = _setup(context)
    sender = q.m.replay.SqsSender(q.sqs, queue)
    lines = _rows(context, at)
    q.sent_bodies += lines
    sender.send(lines)


@given(r"these messages arrive in one batch(?: on (?P<day>[\d-]+))?:")
def step_one_batch(context, day=None):
    _setup(context, getattr(context, "sink", "dynamodb"))
    context.batch_lines = _rows(context, f"{day}T09:30:10Z" if day else None)
    if day:
        from datetime import datetime, timezone

        t = datetime.fromisoformat(f"{day}T09:30:12+00:00").replace(tzinfo=timezone.utc).timestamp()
        context.clock = lambda: t


@given(r"the table refuses the write of trip (?P<trip>t-\d+)(?P<always> every time)?")
def step_refuse(context, trip, always=None):
    _setup(context)
    context.refused = trip
    context.calls.refuse("PutItem", "ProvisionedThroughputExceededException",
                         when=lambda p: p.get("TableName", "").endswith("-trips-completed")
                         and _trip(p["Item"]["trip_id"]["S"]) == context.refused)


use_step_matcher("parse")


use_step_matcher("re")


@given(r'the simulated data set (?P<sid>\S+) "(?P<name>[^"]+)" has been sent to queue (?P<queue>\S+)')
def step_data_set_sent(context, sid, name, queue):
    q = _setup(context)
    folder = q.m.stand_in.ensure(name, context.tmp / "data")
    context.truth = __import__("truth").answer(folder)
    deliveries = q.m.replay.load_deliveries(name, folder.parent)
    if name == "refused-write":
        step_refuse(context, "t-0412")
    q.m.replay.replay(deliveries, q.m.replay.SqsSender(q.sqs, queue), RUN_ID, 0, evidence=context.tmp / "evidence")


use_step_matcher("parse")


@given("the message of trip {trip} is in queue {queue}")
def step_message_in(context, trip, queue):
    q = _setup(context)
    line = {"event_type": "trip_completed", "event_id": f"e-{trip}", "trip_id": trip, "driver_id": "d-0008",
            "dropoff_zone_id": 48, "trip_miles": 3.2, "trip_time_s": 1020, "fare": 24.6, "driver_pay": 17.9,
            "event_time": "2026-11-23T09:30:05Z", "delivery_id": f"dl-e-{trip}", "received_time": "2026-11-23T09:30:06Z"}
    q.m.replay.SqsSender(q.sqs, queue).send([line])


# ---------- the consumer

@when("the consumer has processed the queue")
def step_processed(context):
    _setup(context)
    _drain(context)


@when("the consumer receives the batch")
def step_receive_batch(context):
    _handle(context, _event(context.batch_lines))


@when("the consumer receives the batch twice")
def step_receive_twice(context):
    for i in (1, 2):
        _handle(context, _event(context.batch_lines), SimpleNamespace(aws_request_id=f"req-{i:04d}"))


@when("the consumer receives a batch of {n:d} messages")
def step_receive_n(context, n):
    _setup(context, getattr(context, "sink", "dynamodb"))
    context.batch_lines = [dict(_template_line(i)) for i in range(1, n + 1)]
    _handle(context, _event(context.batch_lines), SimpleNamespace(aws_request_id="req-0001"))


def _template_line(i: int) -> dict:
    return {"event_type": "trip_completed", "event_id": f"e-95{i:02d}", "trip_id": f"t-05{i:02d}",
            "driver_id": "d-0001", "dropoff_zone_id": 161, "trip_miles": 1.0, "trip_time_s": 400, "fare": 10.0,
            "driver_pay": 7.0, "event_time": "2026-11-23T09:31:00Z", "delivery_id": f"dl-e-95{i:02d}",
            "received_time": "2026-11-23T09:31:01Z"}


def _event(lines: list[dict]) -> dict:
    return {"Records": [{"messageId": f"m-{i}", "receiptHandle": f"h-{i}", "body": json.dumps(d, sort_keys=True),
                         "attributes": {}, "eventSource": "aws:sqs"} for i, d in enumerate(lines, 1)]}


@when("the consumer has received the message {n:d} times and failed it each time")
def step_fail_n(context, n):
    q = context.qi
    context.receives = 0
    for _ in range(n):
        calls = _drain(context, rounds=1)
        context.receives += len(calls)
    assert context.receives == n and q.counts.get("failed") == n, (context.receives, q.counts)


@when("{s:d} s have passed since the 5th receive and the consumer polls again")
def step_poll_again(context, s):
    """The drain made the message visible again after each failure (its visibility timeout of 240 s, passed in
    no time): this receive is the one at which SQS moves a message received 5 times."""
    assert s == context.qi.made["visibility_s"], (s, context.qi.made)
    context.after = _drain(context, rounds=1)


@then("within {s:d} s queue {queue} holds the message of trip {trip}")
def step_dlq_holds_trip(context, s, queue, trip):
    assert context.after == [], context.after          # the receive found nothing to hand to the consumer
    bodies = _peek(context, queue)
    assert [json.loads(b)["trip_id"] for b in bodies] == [trip], bodies


def _peek(context, queue: str) -> list[str]:
    """Every body in a queue, received and made visible again (not counted as handled)."""
    sqs, url, found = context.qi.sqs, _url(context, queue), []
    with support.own_calls():
        while True:
            got = sqs.receive_message(QueueUrl=url, MaxNumberOfMessages=10, VisibilityTimeout=30).get("Messages", [])
            if not got:
                break
            found += got
        for m in found:
            sqs.change_message_visibility(QueueUrl=url, ReceiptHandle=m["ReceiptHandle"], VisibilityTimeout=0)
    return [m["Body"] for m in found]


@then("queue {queue} is empty")
def step_queue_empty(context, queue):
    assert _peek(context, queue) == []


# ---------- what was stored

use_step_matcher("re")


@then(r"table (?P<table>\S+) has (?P<n>\d+) items?(?:, for trip (?P<trip>t-\d+))?(?P<fare>, with fare (?P<f>[\d.]+) and a stored_time)?")
def step_table_has(context, table, n, trip=None, fare=None, f=None):
    items = support.items(table)
    assert len(items) == int(n), (len(items), items[:3])
    if trip:
        assert [_trip(i["trip_id"]) for i in items] == [trip], items
    if fare:
        assert float(items[0]["fare"]) == float(f) and items[0]["stored_time"].endswith("Z"), items[0]
        assert items[0]["delivery_id"].endswith(f"dl-{items[0]['event_id'].removeprefix(RUN_ID + '-')}")


@then(r"the consumer counted (?P<s>\d+) stored(?:,| and) (?P<r>\d+) repeats?(?: and (?P<o>\d+) other)?")
def step_counted(context, s, r, o=None):
    c = context.qi.counts
    assert (c.get("stored", 0), c.get("repeat", 0)) == (int(s), int(r)), c
    if o is not None:
        assert c.get("other", 0) == int(o), c
    truth = getattr(context, "truth", None)
    if truth:                                 # the numbers the Then states are the data set's, not the system's
        assert int(s) == truth["completed"], truth["completed"]


use_step_matcher("parse")


@then("the consumer reports only the message of trip {trip} as failed")
def step_only_failed(context, trip):
    (result,) = context.qi.results
    ids = [f["itemIdentifier"] for f in result["batchItemFailures"]]
    which = [i for i, d in enumerate(context.batch_lines, 1) if d["trip_id"] == trip]
    assert ids == [f"m-{which[0]}"], ids


@then("table {table} has items for trips {a} and {b}")
def step_items_for(context, table, a, b):
    assert sorted(_trip(i["trip_id"]) for i in support.items(table)) == sorted([a, b])


# ---------- dead-letter queue and replay

@given("queue {queue} holds {n:d} messages")
def step_dlq_has(context, queue, n):
    q = _setup(context)
    context.dlq_bodies = [json.dumps(_template_line(i), sort_keys=True) for i in range(1, n + 1)]
    with support.own_calls():
        for body in context.dlq_bodies:
            q.sqs.send_message(QueueUrl=_url(context, queue), MessageBody=body)
    context.move_stub = _move_task_stub(context)


def _move_task_stub(context):
    """StartMessageMoveTask and ListMessageMoveTasks in place of AWS: move every message of the source (the
    dead-letter queue) to the destination at once, and report the task COMPLETED with the number moved."""
    sqs, state = context.qi.sqs, {"started": [], "moved": 0}

    def start(params):
        state["started"].append(params)
        src = sqs.get_queue_url(QueueName=params["SourceArn"].rsplit(":", 1)[1])["QueueUrl"]
        dst = sqs.get_queue_url(QueueName=params["DestinationArn"].rsplit(":", 1)[1])["QueueUrl"]
        with support.own_calls():
            while True:
                got = sqs.receive_message(QueueUrl=src, MaxNumberOfMessages=10).get("Messages", [])
                if not got:
                    break
                for m in got:
                    sqs.send_message(QueueUrl=dst, MessageBody=m["Body"])
                    sqs.delete_message(QueueUrl=src, ReceiptHandle=m["ReceiptHandle"])
                    state["moved"] += 1
        return {"TaskHandle": "stub-task"}

    context.calls.answer("StartMessageMoveTask", start)
    context.calls.answer("ListMessageMoveTasks", lambda p: {"Results": [
        {"Status": "COMPLETED", "ApproximateNumberOfMessagesMoved": state["moved"], "SourceArn": p["SourceArn"]}]})
    return state


@given("StartMessageMoveTask is refused with AccessDenied")
def step_move_refused(context):
    context.calls.rules = [r for r in context.calls.rules if r["operation"] != "StartMessageMoveTask"]
    context.calls.refuse("StartMessageMoveTask", "AccessDenied")


@given("the send of the second message fails")
def step_second_send_fails(context):
    seen = {"n": 0}

    def second(params):
        seen["n"] += 1
        return seen["n"] == 2
    context.calls.refuse("SendMessage", "InternalError", times=1, when=second)


@when("replay_dlq.py runs")
def step_replay(context):
    q = _setup(context)
    support.run(context, q.m.dlq.replay, q.sqs, q.n["trip_dlq"], q.n["trip_queue"], sleep=lambda s: None)


@when("the scenario clears the fault and replay_dlq.py runs")
def step_clear_and_replay(context):
    context.refused = None
    _move_task_stub(context)
    step_replay(context)


@then("one message move task was started with a rate of {n:d} messages a second")
def step_one_task(context, n):
    (started,) = context.calls.of("StartMessageMoveTask")
    assert started["MaxNumberOfMessagesPerSecond"] == n, started
    assert started["SourceArn"].endswith("-trip-completed-dlq") and started["DestinationArn"].endswith("-trip-completed")


@then("each of the {n:d} messages was sent to {queue} before it was deleted from {dlq}")
def step_send_before_delete(context, n, queue, dlq):
    made = context.calls.made
    sends = [(i, p["MessageBody"]) for i, (op, p) in enumerate(made) if op == "SendMessage" and p["QueueUrl"].endswith(f"/{queue}")]
    deletes = [i for i, (op, p) in enumerate(made) if op == "DeleteMessage" and p["QueueUrl"].endswith(f"/{dlq}")]
    assert len(sends) == len(deletes) == n, (len(sends), len(deletes))
    assert sorted(b for _, b in sends) == sorted(context.dlq_bodies)
    assert all(s < d for (s, _), d in zip(sends, deletes)), (sends, deletes)


@then("queue {queue} holds {n:d} messages")
def step_queue_holds(context, queue, n):
    assert len(_peek(context, queue)) == n


@then("queue {queue} holds {n:d} message")
def step_queue_holds_one(context, queue, n):
    assert len(_peek(context, queue)) == n


@then("queue {queue} holds {n:d} message, for trip {trip}")
def step_queue_holds_trip(context, queue, n, trip):
    bodies = _peek(context, queue)
    assert [_trip(json.loads(b)["trip_id"]) for b in bodies] == [trip] * n, bodies


# ---------- run.py and the mapping

@given("BATCH_WINDOW_S is {value}")
def step_window(context, value):
    os.environ["BATCH_WINDOW_S"] = value


@given("the consumer's sink is S3")
def step_sink_s3(context):
    context.sink = "s3"
    os.environ["SINK"] = "s3"


@when("run.py creates the queue and the consumer")
def step_run_creates(context):
    m = _mods()
    sink = getattr(context, "sink", "dynamodb")

    def go():
        try:
            window = m.run.batch_window()
        except m.run.SettingError as err:
            print(err.code)
            return 2
        bucket = None
        if sink == "s3":
            import lake

            lake.up(_session())
            bucket = lake.bucket_name(_session())
        context.made = m.run.create(_session(), os.environ["PREFIX"], sink, window, bucket)
        return 0
    support.run(context, go)


def _mapping(context) -> dict:
    (made,) = context.calls.of("CreateEventSourceMapping")
    return made


@then("the consumer's mapping takes batches of {size:d}, reports failed messages and runs at most {copies:d} copies")
def step_mapping(context, size, copies):
    m = _mapping(context)
    assert m["BatchSize"] == size and m["FunctionResponseTypes"] == ["ReportBatchItemFailures"], m
    assert m["ScalingConfig"] == {"MaximumConcurrency": copies}, m
    assert m["FunctionName"].endswith("-trip-completed-consumer") and m["EventSourceArn"].endswith(":recipe-trip-completed")


@then("the mapping's batching window is {s:d} s")
def step_mapping_window(context, s):
    assert _mapping(context)["MaximumBatchingWindowInSeconds"] == s


@then("the queue's visibility timeout is {s:d} seconds")
def step_visibility_timeout(context, s):
    sqs = support.client("sqs")
    url = sqs.get_queue_url(QueueName="recipe-trip-completed")["QueueUrl"]
    got = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["VisibilityTimeout"])["Attributes"]
    assert int(got["VisibilityTimeout"]) == s, got
    (fn,) = [f for f in context.calls.of("CreateFunction") if f["FunctionName"].endswith("-trip-completed-consumer")]
    assert s == 6 * fn["Timeout"] + _mapping(context)["MaximumBatchingWindowInSeconds"]


@then('run.py stops with "{text}"')
def step_run_stops(context, text):
    assert context.result == 2 and text in context.printed, (context.result, context.printed)


@then("no queue or mapping was created")
def step_nothing_created(context):
    names = context.calls.names()
    assert "CreateQueue" not in names and "CreateEventSourceMapping" not in names, names


@then("the mapping takes batches of {size:d} with a batching window of {w:d} s")
def step_s3_mapping(context, size, w):
    m = _mapping(context)
    assert m["BatchSize"] == size and m["MaximumBatchingWindowInSeconds"] == w, m


# ---------- the S3 sink

@then("raw/trip-events/dt={day}/ holds {n:d} objects with different keys, of {lines:d} lines each")
def step_objects(context, day, n, lines):
    q = context.qi
    found = q.s3.list_objects_v2(Bucket=q.bucket, Prefix=f"raw/trip-events/dt={day}/").get("Contents", [])
    keys = [o["Key"] for o in found]
    assert len(keys) == len(set(keys)) == n, keys
    context.objects = [q.s3.get_object(Bucket=q.bucket, Key=k)["Body"].read().decode() for k in keys]
    assert all(len(o.splitlines()) == lines for o in context.objects), context.objects


@then("each line is the delivery line as it was sent")
def step_lines_unchanged(context):
    sent = [json.dumps(d, sort_keys=True) for d in context.batch_lines]
    for body in context.objects:
        assert body.splitlines() == sent, (body, sent)


@then('every PutObject call carried If-None-Match "{value}"')
def step_if_none_match(context, value):
    puts = [p for p in context.calls.of("PutObject") if p["Key"].startswith("raw/trip-events/")]
    assert puts and all(p.get("IfNoneMatch") == value for p in puts), puts


@given("PutObject fails")
def step_put_fails(context):
    context.calls.refuse("PutObject", "InternalError", when=lambda p: p.get("Key", "").startswith("raw/trip-events/"))


@then("the consumer reports all {n:d} messages as failed")
def step_all_failed(context, n):
    (result,) = context.qi.results
    assert [f["itemIdentifier"] for f in result["batchItemFailures"]] == [f"m-{i}" for i in range(1, n + 1)]


# ---------- @live (LIVE=1): in AWS, after `ext-queue-ingest/run.py --target aws --speed 1 --keep`

@given('the simulated data set {sid} "{name}" has been sent to queue {queue} in AWS')
def step_live_sent(context, sid, name, queue):
    context.live_run_id = os.environ["LIVE_RUN_ID"]      # the run id run.py --target aws printed


@then("latency.py joins the {n:d} items to sent.jsonl by delivery_id")
def step_live_join(context, n):
    latency = support.load("queue_ingest_latency", "ext-queue-ingest/latency.py")   # two extensions have a latency.py
    support.run(context, latency.main, ["--run-id", context.live_run_id])
    assert f"{n} latencies" in context.printed, context.printed[-500:]


@then("it prints {n:d} latencies and a p95 under {s:d} s")
def step_live_p95(context, n, s):
    assert f"{n} latencies" in context.printed and ": met" in context.printed, context.printed[-500:]


@then("it prints the batch sizes and the 30-day cost they imply")
def step_live_batches(context):
    assert "mean batch" in context.printed and "30 days at this rate" in context.printed

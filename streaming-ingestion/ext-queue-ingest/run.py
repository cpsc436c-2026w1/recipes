"""ext-queue-ingest: trip events through an SQS queue into DynamoDB (or S3), a dead-letter queue, and a replay.

    uv run python ext-queue-ingest/run.py --target local                     # offline, in moto: no AWS account
    uv run python ext-queue-ingest/run.py --target aws --speed 1 --keep      # in a sandbox: about 2 hours
    uv run python ext-queue-ingest/run.py stop                               # switch the mapping off (no idle polls)
    uv run python ext-queue-ingest/run.py deploy [--no-window]              # create and switch on, send nothing (idle)
    BATCH_WINDOW_S=30 uv run python ext-queue-ingest/run.py --target aws --speed 1 --keep    # 60 s is the default
    uv run python ext-queue-ingest/run.py --target aws --speed 1 --keep --no-window   # comparison only: window 0
    SINK=s3 uv run python ext-queue-ingest/run.py --target aws --speed 1 --keep

`--target aws` creates the queue, its dead-letter queue, the table (DynamoDB sink), the role, the log group, the
function and its event source mapping, all named from PREFIX and tagged with the course tags (scripts/tags.py),
each recorded in state/ledger.jsonl. Then it sends the simulated rush hour with sim/replay.py (one SendMessageBatch
call per due delivery at the hour's pace, `--speed 1`), waits until every completion of the answer from _truth/ is stored, and
runs latency.py. Without `--keep` it switches the mapping off at the end; `clean_up.py` removes everything.

`--target local` does the same inside moto, with `drain()` standing in for Lambda's poller (the README says what
it does not play), and then the replay of the dead-letter queue.

Settings and their reasons: README "Decisions".
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
for folder in (ROOT / "main", ROOT / "scripts", ROOT / "sim", ROOT / "src"):
    sys.path.insert(0, str(folder))

import infra  # noqa: E402
from tags import record, tags  # noqa: E402

FUNCTION_TIMEOUT_S = 30
BATCH_SIZE = 100                 # Lambda takes up to 10,000 from a standard queue; 100 keeps one call short
MAX_COPIES = 2                   # MaximumConcurrency: the lowest Lambda allows
MAX_RECEIVES = 5                 # the least AWS recommends with Lambda
S3_WINDOW_S = 60
WINDOW_RANGE = (30, 60)
DEFAULT_WINDOW_S = 60            # the course's choice (README Decisions, batching)
NO_WINDOW_BATCH = 10             # Lambda refuses a batch size over 10 without a window of at least 1 s
SINKS = ("dynamodb", "s3")


class SettingError(SystemExit):
    pass


def batch_window(env=os.environ) -> int:
    """BATCH_WINDOW_S, 60 unless set; anything but a whole number from 30 to 60 stops the run before any call."""
    text = env.get("BATCH_WINDOW_S", str(DEFAULT_WINDOW_S))
    try:
        value = int(text)
    except ValueError:
        value = -1
    if not WINDOW_RANGE[0] <= value <= WINDOW_RANGE[1]:
        raise SettingError(f"BATCH_WINDOW_S must be 30 to 60 (it is {text!r}): without a window the function is "
                           f"called once per message at this traffic (README Cost, measured)")
    return value


def sink_of(env=os.environ) -> str:
    sink = env.get("SINK", "dynamodb")
    if sink not in SINKS:
        raise SettingError(f"SINK must be one of {', '.join(SINKS)} (it is {sink!r})")
    return sink


def settings(sink: str, window: int) -> dict:
    """The mapping's window and batch size, and the queue's visibility timeout: six times the function's timeout
    plus the window. Window 0 (`--no-window`, for the comparison in README Cost only) takes batches of up to 10:
    Lambda refuses a batch size over 10 without a window of at least 1 s."""
    window = S3_WINDOW_S if sink == "s3" else window
    return {"window_s": window, "batch_size": BATCH_SIZE if window else NO_WINDOW_BATCH,
            "visibility_s": 6 * FUNCTION_TIMEOUT_S + window}


def create(session, prefix: str, sink: str, window: int, bucket: str | None = None,
           wait_enabled: bool = False) -> dict:
    n = infra.names(prefix)
    s = settings(sink, window)
    region, acct = session.region_name, infra.account_id(session)
    sqs, ddb, iam = session.client("sqs"), session.client("dynamodb"), session.client("iam")
    lam, logs = session.client("lambda"), session.client("logs")
    script, clean = "ext-queue-ingest/run.py", "clean_up.py"

    dlq_url, dlq_arn = infra.create_queue(sqs, n["trip_dlq"], keep_days=infra.FAILED_KEEP_DAYS, tags=tags("sqs"))
    record("sqs-queue", n["trip_dlq"], arn=dlq_arn, script=script, clean=clean)
    url, arn = infra.create_queue(sqs, n["trip_queue"], dead_letter_arn=dlq_arn, max_receives=MAX_RECEIVES,
                                  visibility_s=s["visibility_s"], tags=tags("sqs"))
    record("sqs-queue", n["trip_queue"], arn=arn, script=script, clean=clean)

    queue_read = {"Effect": "Allow", "Resource": arn,
                  "Action": ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]}
    if sink == "dynamodb":
        table_arn = infra.create_table(ddb, n["trips_table"], "trip_id", tags=_map(tags("dynamodb")))
        record("dynamodb-table", n["trips_table"], arn=table_arn, script=script, clean=clean)
        sink_rule = {"Effect": "Allow", "Action": ["dynamodb:PutItem"], "Resource": table_arn}
        env = {"SINK": "dynamodb", "TABLE": n["trips_table"]}
    else:
        if not bucket:
            raise SettingError("SINK=s3 needs the lake bucket: run `uv run python scripts/lake.py up` first")
        sink_rule = {"Effect": "Allow", "Action": ["s3:PutObject"], "Resource": f"arn:aws:s3:::{bucket}/raw/trip-events/*"}
        env = {"SINK": "s3", "BUCKET": bucket}
    role_arn = infra.create_role(iam, n["trip_consumer_role"], [queue_read, sink_rule], region, acct,
                                 n["trip_consumer"], tags=_map(tags("iam")))
    record("iam-role", n["trip_consumer_role"], arn=role_arn, script=script, clean=clean)
    infra.create_log_group(logs, n["trip_consumer"], tags=tags("logs"))
    record("log-group", f"/aws/lambda/{n['trip_consumer']}", script=script, clean=clean)
    fn_arn = infra.create_function(lam, n["trip_consumer"], role_arn,
                                   infra.zip_files({"queue_consumer.py": HERE / "queue_consumer.py"}),
                                   "queue_consumer.handler", env, timeout=FUNCTION_TIMEOUT_S, tags=tags("lambda"))
    # a function that was there already (a second run) gets only new code from create_function: set this run's sink
    lam.get_waiter("function_updated_v2").wait(FunctionName=n["trip_consumer"])
    lam.update_function_configuration(FunctionName=n["trip_consumer"], Environment={"Variables": env},
                                      Timeout=FUNCTION_TIMEOUT_S)
    record("lambda-function", n["trip_consumer"], arn=fn_arn, script=script, clean=clean)
    uuid = infra.create_mapping(
        lam, n["trip_consumer"], arn,
        Enabled=True,                                       # on again after `run.py stop`
        BatchSize=s["batch_size"],
        MaximumBatchingWindowInSeconds=s["window_s"],      # a batch over 10 needs a window of at least 1 s
        FunctionResponseTypes=["ReportBatchItemFailures"],  # only the failed messages come back
        ScalingConfig={"MaximumConcurrency": MAX_COPIES},
    )
    record("event-source-mapping", uuid, script=script, clean=clean)
    if wait_enabled:
        # a mapping just made or changed is "Creating"/"Updating" for a while: messages sent then wait, and their
        # latency would count the mapping's start, not the window
        infra.wait_mapping(lam, uuid, state="Enabled", timeout_s=300)
    return {"queue_url": url, "queue_arn": arn, "dlq_url": dlq_url, "dlq_arn": dlq_arn, "mapping": uuid, **s,
            "sink": sink}


def _map(pairs) -> dict:
    return {p["Key"]: p["Value"] for p in pairs} if isinstance(pairs, list) else dict(pairs)


def stop(session, prefix: str) -> int:
    """Switch the mapping off: Lambda stops polling, so the idle receives stop billing. Messages wait in the queue."""
    n = infra.names(prefix)
    lam = session.client("lambda")
    found = lam.list_event_source_mappings(FunctionName=n["trip_consumer"])["EventSourceMappings"]
    for m in found:
        lam.update_event_source_mapping(UUID=m["UUID"], Enabled=False)
        print(f"mapping {m['UUID']} of {n['trip_consumer']}: switched off")
    return len(found)


# ---------- offline stand-in for Lambda's SQS poller

def drain(sqs, url: str, handler, batch_size: int = BATCH_SIZE, visible_again: bool = True, rounds: int = 100,
          context_of=None) -> dict:
    """Receive up to batch_size messages (10 a receive, as SQS allows), call the handler with them, delete what it
    did not report as failed. With visible_again, a failed message is made visible at once: this stands in for its
    visibility timeout (240 s at the default window) passing, which moto would wait out in real time. A message
    received MAX_RECEIVES times moves to the dead-letter queue at its next receive (moto's redrive, as SQS does).
    Returns the counts the handler printed, summed, and the batch sizes."""
    totals = {"stored": 0, "repeat": 0, "other": 0, "failed": 0, "objects": 0}
    sizes = []
    for _ in range(rounds):
        msgs: list[dict] = []
        while len(msgs) < batch_size:
            got = sqs.receive_message(QueueUrl=url, MaxNumberOfMessages=min(10, batch_size - len(msgs)),
                                      AttributeNames=["All"]).get("Messages", [])
            if not got:
                break
            msgs += got
        if not msgs:
            break
        sizes.append(len(msgs))
        event = {"Records": [{"messageId": m["MessageId"], "receiptHandle": m["ReceiptHandle"], "body": m["Body"],
                              "attributes": m["Attributes"], "eventSource": "aws:sqs"} for m in msgs]}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = handler(event, context_of(len(sizes)) if context_of else None)
        for line in out.getvalue().splitlines():
            row = json.loads(line)
            if "batch" in row:
                for k in totals:
                    totals[k] += row.get(k, 0)
        failed = {f["itemIdentifier"] for f in result["batchItemFailures"]}
        done = [m for m in msgs if m["MessageId"] not in failed]
        for i in range(0, len(done), 10):                   # DeleteMessageBatch takes up to 10
            sqs.delete_message_batch(QueueUrl=url, Entries=[{"Id": str(j), "ReceiptHandle": m["ReceiptHandle"]}
                                                            for j, m in enumerate(done[i:i + 10])])
        for m in msgs:
            if m["MessageId"] in failed and visible_again:
                sqs.change_message_visibility(QueueUrl=url, ReceiptHandle=m["ReceiptHandle"], VisibilityTimeout=0)
    return {**totals, "batches": sizes}


def queue_depth(sqs, url: str) -> int:
    a = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["ApproximateNumberOfMessages",
                                                               "ApproximateNumberOfMessagesNotVisible"])["Attributes"]
    return int(a["ApproximateNumberOfMessages"]) + int(a["ApproximateNumberOfMessagesNotVisible"])


def send(session, queue: str, data_set: str, run_id: str, speed: float, evidence: Path) -> dict:
    import replay
    import stand_in

    folder = stand_in.ensure(data_set)
    deliveries = replay.load_deliveries(data_set, folder.parent)
    sender = replay.SqsSender(session.client("sqs"), queue)
    return replay.replay(deliveries, sender, run_id, speed, evidence=evidence / run_id,
                         shift_to=datetime.now(timezone.utc))


def run_local(prefix: str) -> int:
    """The whole extension in moto: create, send the rush hour with one trip refused, see it in the dead-letter
    queue, clear the fault, replay it, and see every completion stored."""
    from moto import mock_aws

    os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
    os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
    os.environ.setdefault("LEDGER", str(ROOT / "state" / "ledger-local.jsonl"))
    import boto3
    import queue_consumer
    import replay_dlq
    import stand_in

    from moto.sqs import models as moto_sqs

    # moto's SQS reads a slow clock helper per message on every receive; the same clock, read directly
    moto_sqs.unix_time_millis, moto_sqs.unix_time = (lambda: time.time() * 1000.0), time.time
    with mock_aws():
        session = boto3.Session(region_name="ca-central-1")
        made = create(session, prefix, "dynamodb", batch_window())
        sqs, ddb = session.client("sqs"), session.client("dynamodb")
        _no_moto_poller()
        n = infra.names(prefix)
        os.environ["TABLE"], os.environ["SINK"] = n["trips_table"], "dynamodb"
        truth = __import__("truth").answer(stand_in.ensure("refused-write"))
        summary = send(session, n["trip_queue"], "refused-write", "local", 0, ROOT / "evidence")
        print(f"sent {summary['sent']} deliveries to {n['trip_queue']} in {summary['calls']} calls")

        refused = {"trip_id": "local-t-0412"}
        real_put = ddb.put_item

        def put_item(**kw):
            if kw["Item"]["trip_id"]["S"] == refused.get("trip_id"):
                raise ddb.exceptions.ProvisionedThroughputExceededException(
                    {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "put in by the demo"}},
                    "PutItem")
            return real_put(**kw)

        ddb.put_item = put_item
        handle = lambda event, context=None: queue_consumer.handler(event, context, ddb=ddb)  # noqa: E731
        got = drain(sqs, made["queue_url"], handle)
        stored = count_items(ddb, n["trips_table"])
        print(f"consumer: {got['stored']} stored, {got['repeat']} repeats, {got['other']} other, "
              f"{got['failed']} failed receives, {len(got['batches'])} batches; table {stored} of "
              f"{truth['completed']} completions; dead-letter queue {queue_depth(sqs, made['dlq_url'])} message")
        refused.clear()
        print("fault cleared; replaying the dead-letter queue")
        replay_dlq.replay(sqs, n["trip_dlq"], n["trip_queue"], copy=True, sleep=lambda s: None)
        drain(sqs, made["queue_url"], handle)
        stored = count_items(ddb, n["trips_table"])
        print(f"table {stored} of {truth['completed']} completions; dead-letter queue "
              f"{queue_depth(sqs, made['dlq_url'])} messages")
        return 0 if stored == truth["completed"] else 1


def _no_moto_poller() -> None:
    """moto hands a message to a mapped function the moment it is sent, a whole batch at once; drain() plays
    Lambda's poller instead, so moto's hand-over is switched off."""
    from moto.sqs.models import sqs_backends

    for backend in sqs_backends.values():
        for regional in backend.values():
            for q in regional.queues.values():
                q.lambda_event_source_mappings.clear()


def count_items(ddb, table: str, run_id: str | None = None) -> int:
    """Every item, or with run_id only that run's (its trip ids start with the run id, sim/replay.py): a Scan reads
    at most 1 MB a page, so a COUNT follows every page. A second run into the same table counts only its own."""
    total, kw = 0, {"TableName": table, "Select": "COUNT", "ConsistentRead": True}
    if run_id:
        kw.update(FilterExpression="begins_with(trip_id, :run)",
                  ExpressionAttributeValues={":run": {"S": f"{run_id}-"}})
    while True:
        page = ddb.scan(**kw)
        total += page["Count"]
        if "LastEvaluatedKey" not in page:
            return total
        kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def wait_stored(ddb, table: str, expected: int, wait_s: float, poll_s: float = 15.0,
                run_id: str | None = None) -> int:
    end = time.time() + wait_s
    while True:
        count = count_items(ddb, table, run_id)
        if count >= expected or time.time() >= end:
            return count
        time.sleep(poll_s)


def deploy(prefix: str, no_window: bool = False, session=None) -> int:
    """Create the queue, function and mapping and wait until the mapping is Enabled; send nothing. What the queue is
    billed from then on is the pollers' idle receives: `latency.py --idle` counts them."""
    import boto3

    session = session or boto3.Session(region_name=os.environ.get("AWS_REGION", "ca-central-1"))
    made = create(session, prefix, "dynamodb", 0 if no_window else batch_window(), wait_enabled=True)
    print(f"deployed at {datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}: mapping {made['mapping']} enabled, window "
          f"{made['window_s']} s, batches of up to {made['batch_size']}; nothing sent")
    return 0


def run_aws(prefix: str, speed: float, keep: bool, data_set: str, wait_s: float, no_window: bool = False) -> int:
    import boto3
    import lake
    import stand_in

    session = boto3.Session(region_name=os.environ.get("AWS_REGION", "ca-central-1"))
    sink, window = sink_of(), (0 if no_window else batch_window())
    bucket = lake.bucket_name(session) if sink == "s3" else None
    made = create(session, prefix, sink, window, bucket, wait_enabled=True)
    print(f"created: queue window {made['window_s']} s, batches of up to {made['batch_size']}, visibility "
          f"{made['visibility_s']} s, sink {sink}" + (" (comparison only: README Cost, batching window)"
                                                      if no_window else ""))
    n = infra.names(prefix)
    run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + f"-{data_set}"
    summary = send(session, n["trip_queue"], data_set, run_id, speed, ROOT / "evidence")
    print(f"sent {summary['sent']} deliveries in {summary['calls']} calls over {summary['seconds']} s "
          f"(run id {run_id}, sent.jsonl under evidence/{run_id}/)")
    truth = __import__("truth").answer(stand_in.ensure(data_set))
    if sink == "dynamodb":
        stored = wait_stored(session.client("dynamodb"), n["trips_table"], truth["completed"], wait_s,
                             run_id=run_id)
        print(f"table {n['trips_table']}: {stored} of {truth['completed']} completions stored")
        print(f"next, about 15 min later: uv run python ext-queue-ingest/latency.py --run-id {run_id}"
              + (" (TRAIL_PREFIX=<the PREFIX of request_count.py up> if a trail counts the calls)" if no_window else ""))
    else:
        time.sleep(S3_WINDOW_S + 30)                      # the last batch's window and its call
        s3 = session.client("s3")
        objects = lines = 0
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix="raw/trip-events/"):
            for o in page.get("Contents", []):
                body = s3.get_object(Bucket=bucket, Key=o["Key"])["Body"].read().decode()
                hits = sum(1 for x in body.splitlines() if f'"{run_id}-' in x)
                objects, lines = objects + (hits > 0), lines + hits
                if hits:                    # scripts/lake.py down deletes the objects recorded here, and no others
                    __import__("tags").record("s3-object", o["Key"], "created", script="ext-queue-ingest/run.py",
                                              clean="scripts/lake.py down")
        print(f"s3://{bucket}/raw/trip-events/: {objects} objects hold {lines} lines of this run "
              f"(of {summary['sent']} sent): the PutObject with If-None-Match went through")
    if not keep:
        stop(session, prefix)
        print("the mapping is off; `uv run python clean_up.py` (it lists what it would delete), then `uv run python clean_up.py --yes` removes the rest")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("action", nargs="?", choices=["run", "stop", "deploy"], default="run",
                   help="deploy: create everything and switch the mapping on, send nothing (to measure idle polling)")
    p.add_argument("--target", choices=["local", "aws"], default="local")
    p.add_argument("--speed", type=float, default=1.0, help="1 = the hour's own pace; 0 = as fast as SQS takes them")
    p.add_argument("--data-set", default="rush-hour")
    p.add_argument("--wait", type=float, default=900.0, help="seconds to wait for the last completion")
    p.add_argument("--keep", action="store_true", help="leave the mapping on at the end")
    p.add_argument("--no-window", action="store_true",
                   help="window 0 and batches of up to 10, to measure what the window saves (README Cost); "
                        "not a setting to keep: one message per call at this traffic")
    args = p.parse_args(argv)
    prefix = os.environ.get("PREFIX", "recipe")
    if args.action == "stop":
        import boto3

        stop(boto3.Session(region_name=os.environ.get("AWS_REGION", "ca-central-1")), prefix)
        return 0
    try:
        if not args.no_window:
            batch_window()             # refuse a bad setting before anything is created
        elif sink_of() == "s3":
            raise SettingError("--no-window is for the DynamoDB sink's comparison; SINK=s3 always gathers for 60 s")
        sink_of()
    except SettingError as err:
        print(err.code, file=sys.stderr)
        return 2
    if args.action == "deploy":
        return deploy(prefix, args.no_window)
    if args.target == "local":
        return run_local(prefix)
    return run_aws(prefix, args.speed, args.keep, args.data_set, args.wait, args.no_window)


if __name__ == "__main__":
    sys.exit(main())

"""Consumer of the queue `$PREFIX-trip-completed`: a Lambda function called by an SQS event source mapping.

Each message body is one delivery line of the rides events (the event's fields plus `delivery_id` and
`received_time`). Two sinks, set by SINK:

- `dynamodb` (default): a `trip_completed` is stored in TABLE as one item keyed by `trip_id` (`fare`, `event_id`,
  `delivery_id`, `stored_time`), on the condition that the trip is not stored yet; a trip that is there already is
  counted as a repeat and its item is left as it is. A `trip_requested` or `trip_accepted` stores nothing and is
  counted as other. A body that is not a valid delivery line, or a write that fails, is reported in
  `batchItemFailures`, so only that message comes back; after 5 receives SQS moves it to the dead-letter queue.
- `s3`: every message of the batch, of every kind, is written unchanged as one line of one new JSON Lines object,
  `raw/trip-events/dt=<UTC day of the call>/<time of the call>-<request id>.jsonl` in BUCKET, with
  `If-None-Match: *`, so a write never replaces an object. If the write fails, the whole batch is reported failed.

The line is checked here with the standard library against the fields of `src/rides/events.py` (the same names,
kinds and "a time needs a zone"; no other field allowed): the function is zipped as it is and runs with what the
Lambda Python runtime has, boto3 and the standard library. tests/test_queue_consumer.py checks that this check and
the Pydantic models accept and refuse the same lines.

Each call prints one JSON line with its counts (`stored`, `repeat`, `other`, `failed`, `objects`), which
`latency.py` and the scenarios read.
"""

import json
import os
import time
import uuid
from datetime import datetime, timezone

import boto3

_clients: dict = {}

COMMON = {"event_id": str, "trip_id": str, "event_time": "time", "delivery_id": str, "received_time": "time"}
KINDS = {
    "trip_requested": {"pickup_zone_id": int},
    "trip_accepted": {"driver_id": str},
    "trip_completed": {"driver_id": str, "dropoff_zone_id": int, "trip_miles": float, "trip_time_s": int,
                       "fare": float, "driver_pay": float},
}


def _client(name: str):
    if name not in _clients:
        _clients[name] = boto3.client(name)
    return _clients[name]


def _is_time(value) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is not None
    except ValueError:
        return False


def _is(value, kind) -> bool:
    if kind == "time":
        return _is_time(value)
    if kind is float:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind is int:
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, kind)


def check_delivery(text: str) -> dict:
    """The delivery as a dict, or ValueError naming the field that is wrong."""
    try:
        line = json.loads(text)
    except json.JSONDecodeError as err:
        raise ValueError(f"not JSON: {err}") from None
    if not isinstance(line, dict):
        raise ValueError("not a JSON object")
    fields = KINDS.get(line.get("event_type"))
    if fields is None:
        raise ValueError(f"event_type: {line.get('event_type')!r} is not one of {sorted(KINDS)}")
    wanted = {**COMMON, **fields, "event_type": str}
    for name, kind in wanted.items():
        if name not in line:
            raise ValueError(f"{name}: missing")
        if not _is(line[name], kind):
            raise ValueError(f"{name}: {line[name]!r} is not a {'time with a zone' if kind == 'time' else kind.__name__}")
    extra = sorted(set(line) - set(wanted))
    if extra:
        raise ValueError(f"{extra[0]}: not a field of {line['event_type']}")
    return line


def iso_ms(epoch_s: float) -> str:
    t = datetime.fromtimestamp(epoch_s, tz=timezone.utc)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def store(ddb, table: str, line: dict, stored_s: float) -> str:
    """'stored', 'repeat' (the trip is there already) or 'other' (not a completion)."""
    if line["event_type"] != "trip_completed":
        return "other"
    item = {"trip_id": {"S": line["trip_id"]}, "fare": {"N": repr(float(line["fare"]))},
            "event_id": {"S": line["event_id"]}, "delivery_id": {"S": line["delivery_id"]},
            "stored_time": {"S": iso_ms(stored_s)}}
    try:
        ddb.put_item(TableName=table, Item=item, ConditionExpression="attribute_not_exists(trip_id)")
        return "stored"
    except ddb.exceptions.ConditionalCheckFailedException:
        return "repeat"


def to_dynamodb(records: list[dict], ddb, table: str, clock) -> tuple[list[dict], dict]:
    counts = {"stored": 0, "repeat": 0, "other": 0, "failed": 0}
    failures = []
    for r in records:
        try:
            counts[store(ddb, table, check_delivery(r["body"]), clock())] += 1
        except Exception as err:    # a body the check refuses or a write that fails: report this message only
            counts["failed"] += 1
            print(json.dumps({"failed": r["messageId"], "error": repr(err)[:300]}))
            failures.append({"itemIdentifier": r["messageId"]})
    return failures, counts


def object_key(called_s: float, request_id: str) -> str:
    t = datetime.fromtimestamp(called_s, tz=timezone.utc)
    return f"raw/trip-events/dt={t:%Y-%m-%d}/{t:%Y%m%dT%H%M%S}{t.microsecond // 1000:03d}Z-{request_id}.jsonl"


def to_s3(records: list[dict], s3, bucket: str, request_id: str, clock) -> tuple[list[dict], dict]:
    body = "".join(r["body"].rstrip("\n") + "\n" for r in records)
    key = object_key(clock(), request_id)
    try:
        s3.put_object(Bucket=bucket, Key=key, Body=body.encode(), ContentType="application/x-ndjson",
                      IfNoneMatch="*")
    except Exception as err:        # nothing was written: every message of the batch comes back
        print(json.dumps({"failed_batch": len(records), "key": key, "error": repr(err)[:300]}))
        return [{"itemIdentifier": r["messageId"]} for r in records], {"objects": 0, "failed": len(records)}
    return [], {"objects": 1, "lines": len(records), "key": key}


def handler(event, context=None, ddb=None, s3=None, clock=time.time):
    """Lambda entry point for an SQS event source mapping with ReportBatchItemFailures."""
    records = event["Records"]
    if os.environ.get("SINK", "dynamodb") == "s3":
        request_id = getattr(context, "aws_request_id", None) or str(uuid.uuid4())
        failures, counts = to_s3(records, s3 or _client("s3"), os.environ["BUCKET"], request_id, clock)
    else:
        failures, counts = to_dynamodb(records, ddb or _client("dynamodb"), os.environ["TABLE"], clock)
    print(json.dumps({"batch": len(records), **counts}))
    return {"batchItemFailures": failures}

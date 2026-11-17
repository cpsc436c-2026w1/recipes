"""Consumer: a Lambda function that reads a batch of Kinesis records and stores each update in DynamoDB.

For every update it records three times, all in epoch milliseconds:
- event_ms:   when the phone took the reading (the phone's clock, from the record);
- entered_ms: when the record entered the stream (Kinesis's approximateArrivalTimestamp);
- arrival_ms: when this consumer stored it (the consumer's clock). Lag = arrival_ms - event_ms.

Decisions (README "Decisions"):
- the write is conditional on the update_id not being stored yet: Lambda reads a stream at least once, so a batch can
  be delivered again, and a repeat must not change the stored arrival time (DDIA 2e ch. 12 "Idempotence");
- a record that fails is reported by its sequence number (ReportBatchItemFailures), and the function stops there, so
  Lambda retries from that record and the shard keeps its order.

This file only needs boto3 and the standard library: it is zipped as it is and runs in the Lambda Python runtime.
"""

import base64
import json
import os
import time
from datetime import datetime

import boto3

_client = None


def _ddb():
    global _client
    if _client is None:
        _client = boto3.client("dynamodb")
    return _client


def _ms(text: str) -> int:
    return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1000)


def store(ddb, table: str, update: dict, entered_ms: int, arrival_ms: int) -> str:
    """Store one update unless its update_id is already there. Returns 'stored' or 'repeat'."""
    event_ms = _ms(update["event_time"])
    item = {
        "update_id": {"S": update["update_id"]},
        "driver_id": {"S": update["driver_id"]},
        "zone_id": {"N": str(update["zone_id"])},
        "event_time": {"S": update["event_time"]},
        "event_ms": {"N": str(event_ms)},
        "entered_ms": {"N": str(entered_ms)},
        "arrival_ms": {"N": str(arrival_ms)},
        "lag_ms": {"N": str(arrival_ms - event_ms)},
    }
    try:
        ddb.put_item(TableName=table, Item=item, ConditionExpression="attribute_not_exists(update_id)")
        return "stored"
    except ddb.exceptions.ConditionalCheckFailedException:
        return "repeat"        # delivered before: keep the first arrival time


def handler(event, context=None, ddb=None, table=None, clock=time.time):
    """Lambda entry point for a Kinesis event source mapping."""
    ddb = ddb or _ddb()
    table = table or os.environ["TABLE"]
    counts = {"stored": 0, "repeat": 0}
    for record in event["Records"]:
        k = record["kinesis"]
        try:
            update = json.loads(base64.b64decode(k["data"]))
            entered_ms = int(float(k["approximateArrivalTimestamp"]) * 1000)
            counts[store(ddb, table, update, entered_ms, int(clock() * 1000))] += 1
        except Exception as err:  # a bad record or a throttled write: report it and stop this batch here
            print(json.dumps({"failed": k["sequenceNumber"], "error": repr(err), **counts}))
            return {"batchItemFailures": [{"itemIdentifier": k["sequenceNumber"]}]}
    print(json.dumps(counts))
    return {"batchItemFailures": []}

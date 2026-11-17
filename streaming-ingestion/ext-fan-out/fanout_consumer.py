"""Consumer of one SQS queue subscribed to the updates topic. The dashboard and history consumers run this same code,
each with its own queue and its own table (TABLE).

For each update it stores, as main/consumer.py does: event_ms (phone), entered_ms (when the queue received the copy,
SQS SentTimestamp) and arrival_ms (this consumer). The write is conditional on update_id, because SNS and SQS standard
deliver at least once. A failed message is reported by its messageId (ReportBatchItemFailures), so only that message
is received again; after maxReceiveCount tries it moves to the queue's dead-letter queue.

Only boto3 and the standard library: zipped as it is for the Lambda Python runtime.
"""

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
    event_ms = _ms(update["event_time"])
    item = {
        "update_id": {"S": update["update_id"]},
        "driver_id": {"S": update["driver_id"]},
        "zone_id": {"N": str(update["zone_id"])},
        "event_time": {"S": update["event_time"]},
        "event_ms": {"N": str(event_ms)},
        "entered_ms": {"N": str(entered_ms)},
        "arrival_ms": {"N": str(arrival_ms)},
    }
    try:
        ddb.put_item(TableName=table, Item=item, ConditionExpression="attribute_not_exists(update_id)")
        return "stored"
    except ddb.exceptions.ConditionalCheckFailedException:
        return "repeat"


def handler(event, context=None, ddb=None, table=None, clock=time.time):
    ddb = ddb or _ddb()
    table = table or os.environ["TABLE"]
    failures, counts = [], {"stored": 0, "repeat": 0}
    for record in event["Records"]:
        try:
            update = json.loads(record["body"])          # raw message delivery: the body is the update itself
            entered_ms = int(record["attributes"]["SentTimestamp"])
            counts[store(ddb, table, update, entered_ms, int(clock() * 1000))] += 1
        except Exception as err:
            print(json.dumps({"failed": record["messageId"], "error": repr(err)}))
            failures.append({"itemIdentifier": record["messageId"]})   # queue order does not matter: go on
    print(json.dumps(counts))
    return {"batchItemFailures": failures}

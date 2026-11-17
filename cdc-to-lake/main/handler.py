"""Lambda function: DynamoDB Streams records in, one change row per record out to Firehose.

This file is the whole function package (it needs only boto3, which the Lambda runtime has), so main/infra.py zips
this one file. Each row says what changed (op), when the table changed (change_time, from the stream) and when the
change reached this function (arrival_time). The lake keeps every row: it is a change log, not a copy of the table
(README, "Decisions": append-only change log).

Environment (set by main/infra.py): DELIVERY_STREAM, the Firehose stream that writes the Iceberg table.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import time
from decimal import Decimal

from boto3.dynamodb.types import TypeDeserializer

KEY = "trip_id"
# The item attributes copied into typed lake columns. Adapt it: change these for your own table, then the same
# names in main/sql/create_table.sql (tests/test_schema.py fails if the two disagree).
ITEM_FIELDS = {"status": "string", "pickup_zone": "string", "fare_quote": "double", "driver_id": "string"}
BATCH_MAX = 500  # PutRecordBatch takes at most 500 records per call (Firehose API reference)

_deserializer = TypeDeserializer()


def to_micros(value) -> int:
    """Firehose wants Iceberg timestamps in microseconds ("For Timestamp data types, you must always send in
    microseconds": https://docs.aws.amazon.com/firehose/latest/dev/apache-iceberg-destination-supp.html).

    Lambda gives ApproximateCreationDateTime as epoch seconds (a number, whole seconds for DynamoDB Streams);
    boto3's GetRecords gives a datetime. Both are accepted so the local run can feed the same function.
    """
    if isinstance(value, dt.datetime):
        return int(value.timestamp() * 1_000_000)
    return int(Decimal(str(value)) * 1_000_000)


def change_row(record: dict, arrival_us: int) -> dict:
    """One stream record -> one lake row. REMOVE rows carry the item's last values from OldImage."""
    d = record["dynamodb"]
    # NEW_AND_OLD_IMAGES on the table (main/infra.py), so a REMOVE still says what was removed.
    image = d.get("NewImage") or d.get("OldImage") or d["Keys"]
    item = {k: _deserializer.deserialize(v) for k, v in image.items()}
    row = {
        KEY: item[KEY],
        "op": record["eventName"],  # INSERT, MODIFY or REMOVE
        "change_time": to_micros(d["ApproximateCreationDateTime"]),
        "arrival_time": arrival_us,
        # DynamoDB Streams keeps the changes to one item in order, with growing sequence numbers, so they order a
        # key's changes when two share the same whole second. Padded to 40 digits so text order is number order.
        "seq": d["SequenceNumber"].rjust(40, "0"),
    }
    for name, kind in ITEM_FIELDS.items():
        v = item.get(name)
        row[name] = float(v) if (kind == "double" and v is not None) else (None if v is None else str(v))
    return row


def send(firehose, stream: str, rows: list[tuple[str, dict]], attempts: int = 3) -> str | None:
    """Send rows in order; return the sequence number of the first record that could not be sent, or None.

    Firehose can accept part of a batch (FailedPutCount > 0), so the failed records are retried on their own.
    """
    for start in range(0, len(rows), BATCH_MAX):
        pending = rows[start:start + BATCH_MAX]
        for attempt in range(attempts):
            resp = firehose.put_record_batch(
                DeliveryStreamName=stream,
                Records=[{"Data": (json.dumps(r) + "\n").encode()} for _, r in pending])
            if resp.get("FailedPutCount", 0) == 0:
                pending = []
                break
            pending = [p for p, res in zip(pending, resp["RequestResponses"]) if res.get("ErrorCode")]
            if attempt + 1 < attempts:                 # no wait after the last call: nothing is sent after it
                time.sleep(0.2 * 2 ** attempt)
        if pending:
            return min(pending, key=lambda p: int(p[0]))[0]
    return None


def handler(event, context, firehose=None, stream: str | None = None, clock=time.time):
    if firehose is None:
        import boto3

        firehose = boto3.client("firehose")
    stream = stream or os.environ["DELIVERY_STREAM"]
    arrival_us = int(clock() * 1_000_000)
    rows = [(r["dynamodb"]["SequenceNumber"], change_row(r, arrival_us)) for r in event["Records"]]
    failed = send(firehose, stream, rows)
    # Partial batch response (the event source mapping sets ReportBatchItemFailures): Lambda retries from the
    # first failed record instead of the whole batch, and the stream keeps the order (Lambda docs,
    # "Configuring partial batch response with DynamoDB and Lambda"). A retried record may arrive twice; the
    # current-state view picks one row per key, so a duplicate does not change any answer.
    return {"batchItemFailures": [{"itemIdentifier": failed}] if failed else []}

"""Lambda function on the table's stream: queue an item for re-embedding only when its embedded text changed.

This file is the whole function package (boto3 only). Environment: QUEUE_URL.

A price or stock change does not change what the item means, so it does not need a new vector; comparing a hash
of the embedded text in the old and the new image skips those changes before any model call.
"""

from __future__ import annotations

import hashlib
import json
import os

from boto3.dynamodb.types import TypeDeserializer

KEY = "id"
EMBEDDED_FIELDS = ("name", "description", "category", "brand")  # Adapt it: the text your vectors stand for
_deser = TypeDeserializer()


def embed_text(item: dict) -> str:
    return ". ".join(str(item.get(f, "")) for f in EMBEDDED_FIELDS)


def text_hash(item: dict) -> str:
    return hashlib.sha256(embed_text(item).encode()).hexdigest()[:16]


def _item(image: dict | None) -> dict | None:
    return None if image is None else {k: _deser.deserialize(v) for k, v in image.items()}


def message_for(record: dict) -> dict | None:
    """The queue message for one stream record, or None when the embedded text did not change."""
    d = record["dynamodb"]
    new, old = _item(d.get("NewImage")), _item(d.get("OldImage"))
    item_id = (new or old)[KEY]
    if record["eventName"] == "REMOVE":
        return {"id": item_id, "op": "REMOVE", "seq": d["SequenceNumber"]}
    h = text_hash(new)
    if old is not None and text_hash(old) == h:
        return None
    return {"id": item_id, "op": "UPSERT", "text_hash": h, "seq": d["SequenceNumber"]}


def handler(event, context, sqs=None, queue_url: str | None = None):
    if sqs is None:
        import boto3

        sqs = boto3.client("sqs")
    queue_url = queue_url or os.environ["QUEUE_URL"]
    messages = [m for m in (message_for(r) for r in event["Records"]) if m]
    for start in range(0, len(messages), 10):  # SendMessageBatch takes at most 10 messages
        batch = messages[start:start + 10]
        resp = sqs.send_message_batch(QueueUrl=queue_url, Entries=[
            {"Id": str(i), "MessageBody": json.dumps(m)} for i, m in enumerate(batch)])
        if resp.get("Failed"):
            # Raise so Lambda retries the batch; the worker skips anything it has already embedded (text_hash).
            raise RuntimeError(f"{len(resp['Failed'])} messages not queued: {resp['Failed'][0]}")
    return {"queued": len(messages), "skipped": len(event["Records"]) - len(messages)}

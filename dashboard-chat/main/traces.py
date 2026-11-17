"""One trace item per question: in DynamoDB for a real run, in a JSON Lines file for a local one.

Table {PREFIX}-chat-traces, partition key request_id, on-demand capacity, items deleted by DynamoDB's time to live
after TRACE_TTL_DAYS (README, "Decisions": traces). The item holds the question and the SQL, because that is what
you read when an answer looks wrong. It holds a hash of the user id, never a key or a token.
"""

from __future__ import annotations

import hashlib
import json
import time
from decimal import Decimal
from pathlib import Path

TRACE_TTL_DAYS = 7  # ours: long enough to debug a week of demos, short enough that old questions disappear


def user_hash(user_id: str) -> str:
    return hashlib.sha256(user_id.encode()).hexdigest()[:16]


def trace_item(ans) -> dict:
    d = ans.to_dict()
    d["user_hash"] = user_hash(d.pop("user_id"))
    d["rows"] = d["rows"][:5]  # a sample of the rows is enough to debug; the full result is not kept
    d["expires_at"] = int(time.time()) + TRACE_TTL_DAYS * 86400
    d["created_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return d


class LocalTraces:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def put(self, ans) -> None:
        with self.path.open("a") as f:
            f.write(json.dumps(trace_item(ans)) + "\n")

    def all(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text().splitlines() if line.strip()]


class DynamoTraces:
    def __init__(self, table_name: str, region: str, client=None):
        import boto3

        self.table = (client or boto3.resource("dynamodb", region_name=region)).Table(table_name)

    def put(self, ans) -> None:
        try:
            self.table.put_item(Item=_to_dynamo(trace_item(ans)))
        except Exception as e:  # a failed trace write never breaks the answer
            print(f"trace write failed: {type(e).__name__}: {e}")

    def get(self, request_id: str) -> dict | None:
        return self.table.get_item(Key={"request_id": request_id}).get("Item")


def _to_dynamo(value):
    """DynamoDB takes Decimal, not float; empty strings are fine in non-key attributes."""
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _to_dynamo(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_dynamo(v) for v in value]
    return value

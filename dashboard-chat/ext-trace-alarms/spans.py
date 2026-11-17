"""Linked traces: one span per model call and per tool call, all under the request that caused them.

Table {PREFIX}-chat-spans, partition key trace_id (= the request id), sort key span_id. One Query by trace_id
returns the whole request in order: the request span "0000" first, then each call ("0001", "0002", ...), each with
parent_id = "0000". That is what "linked" means here: from a slow or failed answer you reach every call it made,
and from a call you reach its request (ext-trace-alarms/README.md).
"""

from __future__ import annotations

import json
import time
from decimal import Decimal
from pathlib import Path

from main.traces import TRACE_TTL_DAYS, user_hash

ROOT_SPAN = "0000"


def spans_of(ans) -> list[dict]:
    """The spans of one answered question (a main.chat.Answer or its dict)."""
    a = ans.to_dict() if hasattr(ans, "to_dict") else ans
    expires = int(time.time()) + TRACE_TTL_DAYS * 86400
    root = {
        "trace_id": a["request_id"], "span_id": ROOT_SPAN, "parent_id": "", "kind": "request", "name": "question",
        "start_ms": 0.0, "duration_ms": a["latency_ms"], "status": a["status"],
        "error": bool(a["status"] == "error"), "tokens_in": a["tokens_in"], "tokens_out": a["tokens_out"],
        "cost_cad": a["cost_cad"], "user_hash": user_hash(a["user_id"]), "question": a["question"],
        "data_version": a.get("data_version", ""), "expires_at": expires,
    }
    out = [root]
    for i, s in enumerate(a["steps"], start=1):
        out.append({
            "trace_id": a["request_id"], "span_id": f"{i:04d}", "parent_id": ROOT_SPAN, "kind": s["kind"],
            "name": s["name"], "start_ms": s["start_ms"], "duration_ms": s["duration_ms"], "status": s["status"],
            "error": s["status"] == "error", "tokens_in": s["tokens_in"], "tokens_out": s["tokens_out"],
            "cost_cad": s["cost_cad"], "detail": s["detail"], "expires_at": expires,
        })
    return out


class LocalSpans:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def put_all(self, spans: list[dict]) -> None:
        with self.path.open("a") as f:
            for s in spans:
                f.write(json.dumps(s) + "\n")


class DynamoSpans:
    def __init__(self, table_name: str, region: str, resource=None):
        import boto3

        self.table = (resource or boto3.resource("dynamodb", region_name=region)).Table(table_name)

    def put_all(self, spans: list[dict]) -> None:
        try:
            with self.table.batch_writer() as w:
                for s in spans:
                    w.put_item(Item=_dec(s))
        except Exception as e:  # tracing never breaks the answer
            print(f"span write failed: {type(e).__name__}: {e}")

    def trace(self, trace_id: str) -> list[dict]:
        from boto3.dynamodb.conditions import Key

        return self.table.query(KeyConditionExpression=Key("trace_id").eq(trace_id))["Items"]

    def scan_all(self) -> list[dict]:
        items, kw = [], {}
        while True:
            page = self.table.scan(**kw)
            items += page["Items"]
            if "LastEvaluatedKey" not in page:
                return items
            kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _dec(v):
    if isinstance(v, float):
        return Decimal(str(v))
    if isinstance(v, dict):
        return {k: _dec(x) for k, x in v.items()}
    return v

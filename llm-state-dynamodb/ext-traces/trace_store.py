"""One trace item per model call or tool call of a request, read back by request in call order.

Table {PREFIX}-llm-traces (main/tables.py): partition key request_id, sort key seq (1, 2, 3, ... in call order).
One Query on request_id returns the whole request, in order, in one read (ext-traces/README.md).

Each item: request_id, seq, kind ("model" or "tool"), name, started_at (epoch ms), duration_ms, status ("ok" or
"error"), error, tokens_in, tokens_out, cost_cad, user_hash, conversation_id, expires_at. The user id is stored as
a hash and the prompt text is not stored (README "Decisions", row "What a trace keeps").
"""

from __future__ import annotations

import hashlib
import time
import uuid
from contextlib import contextmanager
from decimal import Decimal

from boto3.dynamodb.conditions import Key

from config import TRACE_TTL_DAYS


def _dec(x) -> Decimal:
    return Decimal(str(x))


class Call:
    """What the code inside `with tracer.call(...)` fills in."""

    def __init__(self):
        self.tokens_in = 0
        self.tokens_out = 0
        self.cost_cad = 0.0


class Tracer:
    """All the calls of one request. Create one per incoming request."""

    def __init__(self, table_name: str, resource, *, user_id: str, conversation_id: str = "",
                 clock=time.time, ttl_days: int = TRACE_TTL_DAYS):
        self.table = resource.Table(table_name)
        self.request_id = uuid.uuid4().hex
        self.user_hash = hashlib.sha256(user_id.encode()).hexdigest()[:16]
        self.conversation_id = conversation_id
        self.clock = clock
        self.ttl_s = ttl_days * 86400
        self.seq = 0

    @contextmanager
    def call(self, kind: str, name: str):
        """Time one model or tool call and write its item, also when the call raises (status "error")."""
        self.seq += 1
        seq = self.seq  # taken before the call: the order of starting, which is the order a reader wants
        c = Call()
        t0 = self.clock()
        status, error = "ok", ""
        try:
            yield c
        except Exception as e:
            status, error = "error", f"{type(e).__name__}: {e}"[:200]
            raise
        finally:
            t1 = self.clock()
            # README "Decisions", row "When the trace is written": a failure to trace must not fail the answer.
            try:
                self.table.put_item(Item={
                    "request_id": self.request_id, "seq": seq, "kind": kind, "name": name,
                    "started_at": int(t0 * 1000), "duration_ms": _dec(round((t1 - t0) * 1000, 1)),
                    "status": status, "error": error, "tokens_in": c.tokens_in, "tokens_out": c.tokens_out,
                    "cost_cad": _dec(c.cost_cad), "user_hash": self.user_hash,
                    "conversation_id": self.conversation_id, "expires_at": int(t1 + self.ttl_s)})
            except Exception as e:  # noqa: BLE001
                print(f"trace write failed: {e}")


def calls_of(table_name: str, resource, request_id: str) -> list[dict]:
    """Every call of one request, in the order they started. One Query, strongly consistent."""
    table = resource.Table(table_name)
    items, kwargs = [], {"KeyConditionExpression": Key("request_id").eq(request_id), "ConsistentRead": True}
    while True:
        page = table.query(**kwargs)
        items += page["Items"]
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

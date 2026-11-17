"""A user's verdict on an answer, stored on the trace item of that question (ext-feedback/README.md).

A trace says what the model and the engine did. It does not say whether the answer was right; the person who asked
knows. `mark` writes their verdict, "up" or "down", and the time, onto the trace item with that request id:
- a real run: one UpdateItem on the table {PREFIX}-chat-traces, only if the item exists, is the asking user's own
  (its user_hash is the hash of the user who gives the verdict) and has not passed its expiry time;
- a local run: the line of that request in output/traces.jsonl is rewritten, under the same three conditions.
Nothing new is created in AWS, so clean_up.py already removes everything this extension writes.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from main.traces import user_hash

VERDICTS = ("up", "down")


class NoSuchTrace(LookupError):
    """This user has no trace item with this request id that can still be marked: a wrong id, another user's
    answer, or an item past its expiry time (whether or not time to live has removed it yet). One message for
    all of them, so that a refusal does not tell a user that another user's request id exists."""


def _check(verdict: str) -> None:
    if verdict not in VERDICTS:
        raise ValueError(f"a verdict is one of {', '.join(VERDICTS)}; got {verdict!r}")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class LocalFeedback:
    """Verdicts on the trace items of a local run (output/traces.jsonl, written by main/traces.py)."""

    def __init__(self, path: Path):
        self.path = path

    def items(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text().splitlines() if line.strip()]

    def mark(self, request_id: str, verdict: str, user_id: str) -> dict:
        _check(verdict)
        items = self.items()
        found = [i for i in items if i["request_id"] == request_id and i.get("user_hash") == user_hash(user_id)
                 and i.get("expires_at", 0) > time.time()]
        if not found:
            raise NoSuchTrace(f"no trace item of this user for request {request_id} in {self.path}")
        found[0].update(feedback=verdict, feedback_at=_now())  # a later verdict takes the place of an earlier one
        self.path.write_text("".join(json.dumps(i) + "\n" for i in items))
        return found[0]


class DynamoFeedback:
    """Verdicts on the trace items in DynamoDB (table {PREFIX}-chat-traces, key request_id)."""

    def __init__(self, table_name: str, region: str, resource=None):
        import boto3

        self.table = (resource or boto3.resource("dynamodb", region_name=region)).Table(table_name)

    def items(self) -> list[dict]:
        found, kw = [], {}
        while True:  # a Scan: fine for a demo's few hundred items (ext-trace-alarms/analyze.py does the same)
            page = self.table.scan(**kw)
            found += page["Items"]
            if "LastEvaluatedKey" not in page:
                return found
            kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def mark(self, request_id: str, verdict: str, user_id: str) -> dict:
        _check(verdict)
        try:
            # Decision (where the verdict goes): on the trace item, and only if it exists. Without the condition,
            # UpdateItem would create an item that holds a verdict and nothing else.
            # Decision (whose verdict): only the user who asked may mark the answer, so the item's user_hash must
            # be this user's. An item past expires_at is treated as gone, though time to live may not have
            # deleted it yet.
            out = self.table.update_item(
                Key={"request_id": request_id},
                UpdateExpression="SET feedback = :v, feedback_at = :t",
                ConditionExpression="attribute_exists(request_id) AND user_hash = :h AND expires_at > :now",
                ExpressionAttributeValues={":v": verdict, ":t": _now(), ":h": user_hash(user_id),
                                           ":now": int(time.time())},
                ReturnValues="ALL_NEW")
        except self.table.meta.client.exceptions.ConditionalCheckFailedException:
            raise NoSuchTrace(f"no trace item of this user for request {request_id} in {self.table.name}") from None
        return out["Attributes"]


def report(items: list[dict]) -> dict:
    """Counts per verdict, and the answers marked "down" with what you need to see why: question, SQL, reply."""
    marked = [i for i in items if i.get("feedback") in VERDICTS]
    down = sorted((i for i in marked if i["feedback"] == "down"), key=lambda i: i.get("feedback_at", ""))
    return {"answers": len(items), "up": sum(i["feedback"] == "up" for i in marked), "down": len(down),
            "not_marked": len(items) - len(marked),
            "marked_down": [{"request_id": i["request_id"], "question": i["question"], "sql": i.get("sql", ""),
                             "reply": i.get("reply", ""), "status": i.get("status", "")} for i in down]}

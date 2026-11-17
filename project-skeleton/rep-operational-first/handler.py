"""The API of rep-operational-first: one Lambda function behind a function URL, over one DynamoDB table.

    POST /updates   body {"updates": [<event>, ...]}  (at most 100)  -> {"stored": 98, "duplicates": 2, "older": 1}
    GET  /number    ?as_of=2026-11-12T16:00:00.000Z (optional)      -> {"updates_last_hour": 287, ...}
    GET  /current   ?entity_id=st-07                                 -> the newest state of one key

One table, three kinds of item (README, Decisions: "one table"):

    pk = "event#ev-000042"         one per update ever stored; its condition makes a resent copy a no-op
    pk = "minute#2026-11-12T15:04" a counter per minute of event time; GET /number adds up 60 of them
    pk = "key#st-07"               the current state of one key: the newest value and its event time

This file is deployed on its own, so it imports only boto3 and the standard library.
Environment: TABLE (the table name). AWS_REGION is set by Lambda.
"""
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone

import boto3
from botocore.exceptions import ClientError

MAX_BATCH = 100                          # ours: keeps one request well under the 6 MB function URL payload limit
MAX_READS = 5                            # ours: GET /number asks DynamoDB for the same counters at most 5 times
MAX_ID = 64                              # ours: an id is a string of 1 to 64 characters
KEEP_EVENTS_DAYS = 7                     # ours: TTL deletes event items after a week; exports keep the history
TIME_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")   # fixed width: strings sort as times

_client = None


def ddb():
    global _client
    if _client is None:
        _client = boto3.client("dynamodb")
    return _client


def table():
    return os.environ["TABLE"]


class Busy(Exception):
    """DynamoDB kept leaving keys unread (throttled): the caller gets 503, not a number that is too small."""


def _epoch(t: str) -> int:
    return int(datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp())


def valid(u) -> str | None:
    if not isinstance(u, dict):
        return "an update must be a JSON object"
    for f in ("event_id", "entity_id", "event_time", "value"):
        if f not in u:
            return f"missing {f}"
    for f in ("event_id", "entity_id"):
        # an id becomes part of an item's key: a number or a list here would fail inside the write, as a server error
        if not isinstance(u[f], str) or not 0 < len(u[f]) <= MAX_ID:
            return f"{f} must be a string of 1 to {MAX_ID} characters"
    if not TIME_RE.match(str(u["event_time"])):
        return "event_time must look like 2026-11-12T15:04:05.123Z"
    if not isinstance(u["value"], (int, float)) or isinstance(u["value"], bool):
        return "value must be a number"
    return None


def store(u: dict) -> str:
    """Store one update. Returns "stored", "duplicate" (seen before) or "older" (counted, but state kept newer)."""
    t, expires = u["event_time"], str(_epoch(u["event_time"]) + KEEP_EVENTS_DAYS * 86400)
    try:
        # Decision "duplicates": the event item and its minute counter change together or not at all.
        # A second copy of the same event_id fails the condition, so the counter does not count it twice.
        ddb().transact_write_items(TransactItems=[
            {"Put": {"TableName": table(),
                     "Item": {"pk": {"S": f"event#{u['event_id']}"}, "kind": {"S": "event"},
                              "event_id": {"S": u["event_id"]}, "entity_id": {"S": u["entity_id"]},
                              "event_time": {"S": t}, "value": {"N": str(u["value"])},
                              "expires_at": {"N": expires}},
                     "ConditionExpression": "attribute_not_exists(pk)"}},
            {"Update": {"TableName": table(), "Key": {"pk": {"S": f"minute#{t[:16]}"}},
                        "UpdateExpression": "ADD n :one SET kind = :k, expires_at = :x",
                        "ExpressionAttributeValues": {":one": {"N": "1"}, ":k": {"S": "minute"},
                                                      ":x": {"N": expires}}}},
        ])
    except ClientError as e:
        reasons = [r.get("Code") for r in e.response.get("CancellationReasons", [])]
        if e.response["Error"]["Code"] == "TransactionCanceledException" and reasons[:1] == ["ConditionalCheckFailed"]:
            return "duplicate"
        raise
    try:
        # Decision "out of order": the state moves only forward in event time, whatever order updates arrive in.
        ddb().update_item(
            TableName=table(), Key={"pk": {"S": f"key#{u['entity_id']}"}},
            UpdateExpression="SET kind = :k, entity_id = :e, #v = :v, last_event_time = :t, last_event_id = :i",
            ConditionExpression="attribute_not_exists(last_event_time) OR last_event_time < :t",
            ExpressionAttributeNames={"#v": "value"},
            ExpressionAttributeValues={":k": {"S": "state"}, ":e": {"S": u["entity_id"]},
                                       ":v": {"N": str(u["value"])}, ":t": {"S": t}, ":i": {"S": u["event_id"]}})
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        return "older"
    return "stored"


def window(as_of: datetime) -> tuple[datetime, datetime]:
    """The 60 whole minutes before the minute of as_of (same rule as skeleton/number.py)."""
    end = as_of.astimezone(timezone.utc).replace(second=0, microsecond=0)
    return end - timedelta(hours=1), end


def number(as_of: datetime) -> dict:
    """Updates in the last hour: add up 60 minute counters with one BatchGetItem (it takes up to 100 keys)."""
    start, end = window(as_of)
    keys = [{"pk": {"S": f"minute#{(start + timedelta(minutes=i)):%Y-%m-%dT%H:%M}"}} for i in range(60)]
    # Decision "reads": eventually consistent reads bill half of a strongly consistent one; a dashboard number
    # that may miss writes of the last second or so is fine.
    request, total, tries = {table(): {"Keys": keys, "ProjectionExpression": "n"}}, 0, 0
    while request:
        r = ddb().batch_get_item(RequestItems=request)
        total += sum(int(it["n"]["N"]) for it in r["Responses"].get(table(), []))
        request = r.get("UnprocessedKeys") or {}
        if request:
            tries += 1
            if tries == MAX_READS:
                # Limit the retries (REL05-BP03): without a limit this loop ends when Lambda's timeout ends it
                raise Busy
            time.sleep(min(0.05 * 2 ** tries, 1))
    fmt = "%Y-%m-%dT%H:%M:%S.000Z"
    return {"updates_last_hour": total, "window_start": f"{start:{fmt}}", "window_end": f"{end:{fmt}}"}


def current(entity_id: str) -> dict | None:
    r = ddb().get_item(TableName=table(), Key={"pk": {"S": f"key#{entity_id}"}})
    it = r.get("Item")
    if not it:
        return None
    return {"entity_id": it["entity_id"]["S"], "value": float(it["value"]["N"]),
            "last_event_time": it["last_event_time"]["S"], "last_event_id": it["last_event_id"]["S"]}


def _reply(status: int, body: dict) -> dict:
    return {"statusCode": status, "headers": {"content-type": "application/json"}, "body": json.dumps(body)}


ROUTES = ("/updates", "/number", "/current")
COUNTS = ("stored", "duplicates", "older", "updates_last_hour")


def handler(event, context=None):
    """What Lambda calls: answer the request, then write one log line about it.

    The line has Lambda's request id (to find the REPORT line of the same call), the method, the route, the status
    and the counts of the reply. It has no id of an update or a key, and no text a caller typed: a path that is
    not one of the three routes is written as "other".
    """
    reply = route(event)
    body = json.loads(reply["body"])
    path = event.get("rawPath", "/")
    print(json.dumps({"request_id": getattr(context, "aws_request_id", None),
                      "method": event.get("requestContext", {}).get("http", {}).get("method", "GET"),
                      "route": path if path in ROUTES else "other", "status": reply["statusCode"],
                      **{k: body[k] for k in COUNTS if k in body}}))
    return reply


def route(event):
    """Function URL events use payload format 2.0: requestContext.http.method, rawPath, queryStringParameters."""
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET")
    path = event.get("rawPath", "/")
    params = event.get("queryStringParameters") or {}
    if method == "POST" and path == "/updates":
        body = event.get("body") or "{}"
        if event.get("isBase64Encoded"):
            import base64
            body = base64.b64decode(body).decode()
        try:
            updates = json.loads(body).get("updates", [])
        except (json.JSONDecodeError, AttributeError):
            return _reply(400, {"error": "body must be JSON: {\"updates\": [...]}"})
        if not isinstance(updates, list) or not 0 < len(updates) <= MAX_BATCH:
            return _reply(400, {"error": f"send 1 to {MAX_BATCH} updates"})
        for i, u in enumerate(updates):
            problem = valid(u)
            if problem:
                return _reply(400, {"error": f"update {i}: {problem}"})   # nothing is stored when one is bad
        outcome = {"stored": 0, "duplicates": 0, "older": 0}
        for u in updates:
            r = store(u)
            outcome[{"stored": "stored", "duplicate": "duplicates", "older": "older"}[r]] += 1
        return _reply(200, outcome)
    if method == "GET" and path == "/number":
        as_of = params.get("as_of")
        # Only the form of the example: a time without "Z" would be read as the function's local time, and a time
        # at the edge of the calendar (year 1) has no hour before it. Either would fail the function, and the caller
        # would get a bare 502.
        try:
            if as_of and not TIME_RE.match(as_of):
                raise ValueError(as_of)
            when = datetime.fromisoformat(as_of.replace("Z", "+00:00")) if as_of else datetime.now(timezone.utc)
            window(when)
        except (ValueError, OverflowError):
            return _reply(400, {"error": "as_of must look like 2026-11-12T16:00:00.000Z"})
        try:
            return _reply(200, number(when))
        except Busy:
            return _reply(503, {"error": "the table is busy; ask again in a moment"})
    if method == "GET" and path == "/current":
        found = current(params.get("entity_id", ""))
        return _reply(200, found) if found else _reply(404, {"error": "no such key"})
    return _reply(404, {"error": f"no route {method} {path}"})

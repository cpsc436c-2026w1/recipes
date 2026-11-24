"""Second consumer of the updates stream: counts per zone in 5-minute tumbling windows by event time.

Rule for late records (README "Decisions"):
- the watermark is the newest event time seen so far on the shard, minus a close delay (CLOSE_DELAY_S, default 120 s);
- a window is closed once the watermark has reached or passed its end;
- a record for a closed window is late. LATE_RULE=drop (default) leaves the count as it was and adds 1 to the window's
  late_dropped, so the drops are counted; LATE_RULE=correct adds it to the count and adds 1 to corrections, so a reader
  that already took the window's first value knows it changed.

The watermark used for a batch is the one stored before the batch; it moves on after the batch, as in Spark's
micro-batches. Each update is applied once: a marker item seen#<update_id> is written in the same transaction as the
count, on the condition that it does not exist yet (DDIA 2e ch. 12 "Idempotence"; L14 request id).

State lives in one DynamoDB table (key "pk"):
    window#<zone>#<start>   count, late_dropped, corrections, zone_id, window_start, window_end
    watermark#<shard>       max_event_ms
    seen#<update_id>        expires_at (the table's TTL attribute: markers are deleted after MARKER_DAYS)

Only boto3 and the standard library: zipped as it is for the Lambda Python runtime.
"""

import base64
import json
import os
import time
from datetime import datetime, timezone

import boto3

WINDOW_MS = 5 * 60 * 1000
MARKER_DAYS = 2
_client = None


def _ddb():
    global _client
    if _client is None:
        _client = boto3.client("dynamodb")
    return _client


def _ms(text: str) -> int:
    return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1000)


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def window_of(event_ms: int) -> tuple[int, int]:
    start = event_ms - event_ms % WINDOW_MS
    return start, start + WINDOW_MS


def get_max_event(ddb, table: str, shard: str) -> int | None:
    item = ddb.get_item(TableName=table, Key={"pk": {"S": f"watermark#{shard}"}}, ConsistentRead=True).get("Item")
    return int(item["max_event_ms"]["N"]) if item else None


def advance(ddb, table: str, shard: str, max_event_ms: int) -> None:
    """Move the shard's newest event time forward, never back."""
    try:
        ddb.update_item(TableName=table, Key={"pk": {"S": f"watermark#{shard}"}},
                        UpdateExpression="SET max_event_ms = :m",
                        ConditionExpression="attribute_not_exists(max_event_ms) OR max_event_ms < :m",
                        ExpressionAttributeValues={":m": {"N": str(max_event_ms)}})
    except ddb.exceptions.ConditionalCheckFailedException:
        pass


def apply(ddb, table: str, update: dict, watermark_ms: int | None, rule: str, now_s: float) -> str:
    """Count one update in its window. Returns on_time, late_dropped, late_corrected or repeat."""
    event_ms = _ms(update["event_time"])
    start, end = window_of(event_ms)
    late = watermark_ms is not None and end <= watermark_ms
    if not late:
        outcome, add = "on_time", "#c :one"
    elif rule == "correct":
        outcome, add = "late_corrected", "#c :one, corrections :one"
    else:
        outcome, add = "late_dropped", "late_dropped :one"
    zone = str(update["zone_id"])
    marker = {"Put": {
        "TableName": table,
        "Item": {"pk": {"S": f"seen#{update['update_id']}"},
                 "expires_at": {"N": str(int(now_s) + MARKER_DAYS * 86400)}},
        "ConditionExpression": "attribute_not_exists(pk)"}}
    count = {"Update": {
        "TableName": table,
        "Key": {"pk": {"S": f"window#{zone}#{_iso(start)}"}},
        "UpdateExpression": f"SET zone_id = :z, window_start = :s, window_end = :e ADD {add}",
        "ExpressionAttributeNames": {"#c": "count"} if "#c" in add else {},
        "ExpressionAttributeValues": {":z": {"N": zone}, ":s": {"S": _iso(start)}, ":e": {"S": _iso(end)},
                                      ":one": {"N": "1"}}}}
    if not count["Update"]["ExpressionAttributeNames"]:
        del count["Update"]["ExpressionAttributeNames"]
    try:
        ddb.transact_write_items(TransactItems=[marker, count])
        return outcome
    except ddb.exceptions.TransactionCanceledException as err:
        reasons = err.response.get("CancellationReasons", [])
        if reasons and reasons[0].get("Code") == "ConditionalCheckFailed":
            return "repeat"            # this update was counted before: a redelivered record
        raise


def handler(event, context=None, ddb=None, table=None, rule=None, close_delay_s=None, clock=time.time):
    ddb = ddb or _ddb()
    table = table or os.environ["TABLE"]
    rule = rule or os.environ.get("LATE_RULE", "drop")
    close_ms = int(float(close_delay_s if close_delay_s is not None else os.environ.get("CLOSE_DELAY_S", 120)) * 1000)
    records = event["Records"]
    if not records:
        return {"batchItemFailures": []}
    shard = records[0]["eventID"].split(":")[0]          # one batch comes from one shard
    newest = get_max_event(ddb, table, shard)
    watermark = None if newest is None else newest - close_ms
    counts: dict[str, int] = {}
    batch_max = newest or 0
    for record in records:
        k = record["kinesis"]
        try:
            update = json.loads(base64.b64decode(k["data"]))
            outcome = apply(ddb, table, update, watermark, rule, clock())
            counts[outcome] = counts.get(outcome, 0) + 1
            batch_max = max(batch_max, _ms(update["event_time"]))
        except Exception as err:
            if batch_max:
                advance(ddb, table, shard, batch_max)     # the records before this one were counted
            print(json.dumps({"failed": k["sequenceNumber"], "error": repr(err), **counts}))
            return {"batchItemFailures": [{"itemIdentifier": k["sequenceNumber"]}]}
    advance(ddb, table, shard, batch_max)
    print(json.dumps({"watermark": _iso(watermark) if watermark else None, **counts}))
    return {"batchItemFailures": []}


def read_windows(ddb, table: str) -> tuple[list[dict], int | None]:
    """Every window, and the newest event time seen, taken as the smallest over the shards (a slow shard holds the
    watermark back). A window is final once that time minus the close delay has reached or passed its end."""
    windows, marks, kwargs = [], [], {"TableName": table}
    while True:
        page = ddb.scan(**kwargs)
        for it in page["Items"]:
            pk = it["pk"]["S"]
            if pk.startswith("window#"):
                windows.append({"zone_id": int(it["zone_id"]["N"]), "window_start": it["window_start"]["S"],
                                "window_end": it["window_end"]["S"],
                                "count": int(it.get("count", {"N": "0"})["N"]),
                                "late_dropped": int(it.get("late_dropped", {"N": "0"})["N"]),
                                "corrections": int(it.get("corrections", {"N": "0"})["N"])})
            elif pk.startswith("watermark#"):
                marks.append(int(it["max_event_ms"]["N"]))
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    windows.sort(key=lambda w: (w["window_start"], w["zone_id"]))
    return windows, (min(marks) if marks else None)

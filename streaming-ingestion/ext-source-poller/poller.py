"""The feed poller: a Lambda function that EventBridge Scheduler calls every 15 minutes.

A partner publishes the completed trips of each 15-minute slot as one JSON Lines file and lists it in
`manifest.json` ({"slots": [{"slot": "2026-11-22T08:00Z", "file": "...", "published_time": "..."}]}; the
publication time is optional). Each run:

1. takes the run log's `lock` item for job `source-poller` (a conditional write, lock_until = now + 300 s) and
   exits if the previous run still holds it;
2. reads the manifest from FEED_URL (an `s3://bucket/prefix/` for the simulated feed, an `https://` address for a
   partner); with FILE_PATTERN set and no manifest, it builds each due slot's file name and asks for it with HEAD;
3. reads the slot items of table SLOTS_TABLE with one Query per UTC date from 25 h 15 min before the run to the
   run's own date (two dates, three in the first 1 h 15 min after midnight);
4. works through the listed slots that ended in the last 25 hours and are not `loaded` or `late`, oldest first,
   skipping one published (or first listed) more than 24 h after its end;
5. claims a slot with a conditional put (no item yet, or `failed`, or `loading` with a claim older than 15 min),
   reads its file, turns each `trip_completed` into the update the main consumer stores, sends the records to
   STREAM in PutRecords calls of at most 500 keyed by `trip_id`, sends again only the refused records, and marks
   the slot `loaded` (or `late`: published, or first listed, more than 30 min after its end) with its count;
   an error after the claim marks it `failed` with the error text, and the next run claims it again;
6. starts no new slot when less than 45 s of the function's time is left.

It prints one JSON line per slot (latency.py reads `published_time` and `last_accepted`) and one per run.
Only boto3 and the standard library: zipped as it is for the Lambda Python runtime.
"""

import json
import os
import random
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import boto3

SLOT = timedelta(minutes=15)
LOOK_BACK = timedelta(hours=25)       # 24 h of lateness the feed may have, plus an hour for later polls
LATEST = timedelta(hours=24)          # a file published later than this after its slot's end is not loaded
LATE_AFTER = timedelta(minutes=30)
STALE_CLAIM = timedelta(minutes=15)
LOCK_S = 300                          # the function's timeout
RESERVE_S = 45                        # no new slot when less than this is left
BATCH = 500                           # PutRecords takes at most 500 records a call
MAX_ATTEMPTS = 5
JOB = "source-poller"
DONE = ("loaded", "late")

_clients: dict = {}


def _client(name: str):
    if name not in _clients:
        _clients[name] = boto3.client(name)
    return _clients[name]


def stamp(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def stamp_ms(t: datetime) -> str:
    t = t.astimezone(timezone.utc)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def slot_name(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def parse(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def query_dates(now: datetime) -> list[str]:
    """The UTC dates of the slot items a run reads: from 25 h 15 min before it to its own date."""
    first, last = (now - LOOK_BACK - SLOT).date(), now.date()
    return [(first + timedelta(days=i)).isoformat() for i in range((last - first).days + 1)]


# ---------- the feed

class Feed:
    """Reads the partner's manifest and files: s3://bucket/prefix/ or https://host/path/."""

    def __init__(self, url: str, s3=None, opener=urllib.request.urlopen, pattern: str = ""):
        self.url, self.pattern, self.opener = url if url.endswith("/") else url + "/", pattern, opener
        self.s3 = s3

    def _get(self, name: str) -> bytes:
        if self.url.startswith("s3://"):
            bucket, _, prefix = self.url[5:].partition("/")
            return self.s3.get_object(Bucket=bucket, Key=prefix + name)["Body"].read()
        with self.opener(self.url + name, timeout=30) as resp:
            return resp.read()

    def _exists(self, name: str) -> bool:
        req = urllib.request.Request(self.url + name, method="HEAD")
        try:
            with self.opener(req, timeout=30) as resp:
                return 200 <= resp.status < 300
        except urllib.error.HTTPError as err:
            if err.code == 404:
                return False
            raise

    def listed(self, now: datetime) -> list[dict]:
        """Manifest entries; with a file pattern, one entry per due slot whose file answers a HEAD."""
        if not self.pattern:
            return json.loads(self._get("manifest.json"))["slots"]
        out, t = [], _floor(now - LOOK_BACK)
        while t + SLOT <= now:
            name = t.strftime(self.pattern)
            if self._exists(name):
                out.append({"slot": slot_name(t), "file": name})
            t += SLOT
        return out

    def read(self, name: str) -> bytes:
        return self._get(name)


def _floor(t: datetime) -> datetime:
    return t.replace(minute=t.minute - t.minute % 15, second=0, microsecond=0)


def records_of(data: bytes, sent: datetime) -> list[dict]:
    """The updates of a slot file: each trip_completed as the main consumer stores it. A line that is not JSON or
    has no event id raises ValueError naming the line."""
    out = []
    for n, text in enumerate(data.decode().splitlines(), 1):
        if not text.strip():
            continue
        try:
            e = json.loads(text)
            if e["event_type"] != "trip_completed":
                continue
            out.append({"update_id": e["event_id"], "trip_id": e["trip_id"], "driver_id": e["driver_id"],
                        "zone_id": int(e["dropoff_zone_id"]), "event_time": e["event_time"],
                        "sent_time": stamp_ms(sent)})
        except (ValueError, KeyError, TypeError) as err:
            raise ValueError(f"line {n}: {type(err).__name__}: {err}") from None
    return out


# ---------- the stream

def send(kinesis, stream: str, records: list[dict], sleep=time.sleep, wall=lambda: datetime.now(timezone.utc)) -> dict:
    """PutRecords calls of at most 500, keyed by trip_id; the records a call refused are gathered and sent again,
    after a random wait under a doubling limit, at most MAX_ATTEMPTS times in all. Returns the calls' sizes and the
    time the last record was accepted."""
    pending = [{"Data": json.dumps(r).encode(), "PartitionKey": r["trip_id"]} for r in records]
    calls, last = [], None
    for attempt in range(MAX_ATTEMPTS):
        refused = []
        for i in range(0, len(pending), BATCH):
            chunk = pending[i:i + BATCH]
            calls.append(len(chunk))
            resp = kinesis.put_records(StreamName=stream, Records=chunk)
            if resp.get("FailedRecordCount", 0) < len(chunk):
                last = wall()
            refused += [e for e, res in zip(chunk, resp["Records"]) if res.get("ErrorCode")]
        if not refused:
            return {"calls": calls, "last_accepted": last}
        pending = refused
        if attempt + 1 < MAX_ATTEMPTS:
            sleep(random.uniform(0, 0.1 * 2 ** attempt))
    raise RuntimeError(f"{len(pending)} records still refused after {MAX_ATTEMPTS} attempts")


# ---------- the run log lock

def take_lock(ddb, table: str, now: datetime) -> bool:
    try:
        ddb.put_item(TableName=table, Item={"job": {"S": JOB}, "run_id": {"S": "lock"},
                                            "lock_until": {"S": stamp(now + timedelta(seconds=LOCK_S))}},
                     ConditionExpression="attribute_not_exists(run_id) OR lock_until < :now",
                     ExpressionAttributeValues={":now": {"S": stamp(now)}})
        return True
    except ddb.exceptions.ConditionalCheckFailedException:
        return False


def release_lock(ddb, table: str, now: datetime, summary: dict, run_id: str | None = None) -> None:
    ddb.put_item(TableName=table, Item={"job": {"S": JOB}, "run_id": {"S": run_id or stamp(now)},
                                        "run_state": {"S": "finished"}, "objects": {"N": str(summary["loaded"])},
                                        "started_at": {"S": stamp(now)}, "ended_at": {"S": summary["ended_at"]}})
    ddb.delete_item(TableName=table, Key={"job": {"S": JOB}, "run_id": {"S": "lock"}})


# ---------- slot items

def read_items(ddb, table: str, now: datetime) -> dict[str, dict]:
    found = {}
    for day in query_dates(now):
        kw = {"TableName": table, "KeyConditionExpression": "slot_day = :d",
              "ExpressionAttributeValues": {":d": {"S": day}}, "ConsistentRead": True}
        while True:
            page = ddb.query(**kw)
            for it in page["Items"]:
                found[it["slot_start"]["S"]] = {k: next(iter(v.values())) for k, v in it.items()}
            if "LastEvaluatedKey" not in page:
                break
            kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    return found


def claim(ddb, table: str, slot: str, now: datetime, item: dict | None, published: str | None) -> bool:
    """The conditional put: no item, or failed, or a loading claim older than 15 min. True if this run won."""
    first_seen = (item or {}).get("first_seen_time") or stamp(now)
    new = {"slot_day": {"S": slot[:10]}, "slot_start": {"S": slot}, "slot_state": {"S": "loading"},
           "claimed_at": {"S": stamp(now)}, "first_seen_time": {"S": first_seen}}
    if published:
        new["published_time"] = {"S": published}
    try:
        ddb.put_item(TableName=table, Item=new,
                     ConditionExpression="attribute_not_exists(slot_start) OR slot_state = :failed "
                                         "OR (slot_state = :loading AND claimed_at < :stale)",
                     ExpressionAttributeValues={":failed": {"S": "failed"}, ":loading": {"S": "loading"},
                                                ":stale": {"S": stamp(now - STALE_CLAIM)}})
        return True
    except ddb.exceptions.ConditionalCheckFailedException:
        return False


def mark(ddb, table: str, slot: str, state: str, **fields) -> None:
    names = {"slot_state": state, **fields}
    ddb.update_item(TableName=table, Key={"slot_day": {"S": slot[:10]}, "slot_start": {"S": slot}},
                    UpdateExpression="SET " + ", ".join(f"{k} = :{k}" for k in names),
                    ExpressionAttributeValues={f":{k}": ({"N": str(v)} if isinstance(v, int) else {"S": str(v)})
                                               for k, v in names.items()})


# ---------- one run

def due(entries: list[dict], items: dict, now: datetime) -> list[dict]:
    """Listed slots that ended in the last 25 h, are not loaded, and were published within 24 h of their end."""
    out = []
    for e in entries:
        start = parse(e["slot"])
        end = start + SLOT
        if not (now - LOOK_BACK <= end <= now):
            continue
        item = items.get(slot_name(start))
        if item and item.get("slot_state") in DONE:
            continue
        seen = e.get("published_time") or (item or {}).get("first_seen_time") or stamp(now)
        if parse(seen) - end > LATEST:
            continue
        out.append({**e, "slot": slot_name(start)})
    return sorted(out, key=lambda e: e["slot"])


def run(now: datetime, *, ddb, kinesis, feed: Feed, slots_table: str, run_log: str, stream: str,
        remaining_s=lambda: LOCK_S, wall=lambda: datetime.now(timezone.utc), sleep=time.sleep,
        stop_after_claim: bool = False, run_id: str | None = None) -> dict:
    """One run. `run_id` keys its run-log item: the handler adds the Lambda request id to the start time, so two
    runs that start in the same second (a scheduled one and one started by hand) keep an item each."""
    run_id = run_id or stamp(now)
    summary = {"run": stamp(now), "run_id": run_id, "loaded": 0, "late": 0, "failed": 0, "skipped_claims": 0,
               "left_for_next": 0}
    if not take_lock(ddb, run_log, now):
        print(json.dumps({**summary, "run_state": "skipped", "why": "the previous run holds the lock"}))
        return {**summary, "run_state": "skipped"}
    try:
        items = read_items(ddb, slots_table, now)
        todo = due(feed.listed(now), items, now)
    except Exception as err:                   # the manifest or the table: nothing was claimed; say so and stop
        summary["ended_at"] = stamp(wall())
        ddb.put_item(TableName=run_log, Item={"job": {"S": JOB}, "run_id": {"S": run_id},
                                              "run_state": {"S": "failed"}, "failed_step": {"S": "list"},
                                              "error_text": {"S": f"{type(err).__name__}: {err}"[:500]},
                                              "started_at": {"S": stamp(now)}, "ended_at": {"S": summary["ended_at"]}})
        ddb.delete_item(TableName=run_log, Key={"job": {"S": JOB}, "run_id": {"S": "lock"}})
        raise
    for n, e in enumerate(todo):
        if remaining_s() < RESERVE_S:
            summary["left_for_next"] = len(todo) - n
            break
        slot, item = e["slot"], items.get(e["slot"])
        if not claim(ddb, slots_table, slot, now, item, e.get("published_time")):
            summary["skipped_claims"] += 1
            continue
        if stop_after_claim:                 # a scenario's stand-in for a run that stops (timeout, crash) here
            raise SystemExit("stopped after the claim")
        seen = e.get("published_time") or (item or {}).get("first_seen_time") or stamp(now)
        try:
            records = records_of(feed.read(e["file"]), wall())
            sent = send(kinesis, stream, records, sleep=sleep, wall=wall)
        except Exception as err:               # noqa: BLE001  the file or the stream: the next run claims it again
            mark(ddb, slots_table, slot, "failed", error_text=f"{type(err).__name__}: {err}"[:500])
            summary["failed"] += 1
            print(json.dumps({"slot": slot, "slot_state": "failed", "error_text": str(err)[:300]}))
            continue
        state = "late" if parse(seen) - (parse(slot) + SLOT) > LATE_AFTER else "loaded"
        mark(ddb, slots_table, slot, state, record_total=len(records), loaded_at=stamp(wall()))
        summary["loaded" if state == "loaded" else "late"] += 1
        print(json.dumps({"slot": slot, "slot_state": state, "record_total": len(records), "calls": sent["calls"],
                          "published_time": e.get("published_time"), "first_seen_time": seen,
                          "last_accepted": stamp_ms(sent["last_accepted"]) if sent["last_accepted"] else None}))
    summary["loaded"] += summary["late"]
    summary["ended_at"] = stamp(wall())
    release_lock(ddb, run_log, now, summary, run_id)
    print(json.dumps({**summary, "run_state": "finished"}))
    return {**summary, "run_state": "finished"}


def handler(event, context=None):
    """Lambda entry point. The schedule passes {"scheduled_time": "<aws.scheduler.scheduled-time>"}; the run's clock
    is the function's own (a late delivery of the schedule judges slots by when it really runs)."""
    now = datetime.now(timezone.utc)
    remaining = (lambda: context.get_remaining_time_in_millis() / 1000) if context else (lambda: LOCK_S)
    feed = Feed(os.environ["FEED_URL"], s3=_client("s3"), pattern=os.environ.get("FILE_PATTERN", ""))
    return run(now, ddb=_client("dynamodb"), kinesis=_client("kinesis"), feed=feed,
               slots_table=os.environ["SLOTS_TABLE"], run_log=os.environ["RUN_LOG"], stream=os.environ["STREAM"],
               remaining_s=remaining,
               run_id=f"{stamp(now)}-{context.aws_request_id}" if getattr(context, "aws_request_id", None) else None)

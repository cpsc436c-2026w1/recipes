"""The page function `<PREFIX>-page`: one HTML page at `/` and its data at `/api/zone-counts`, read from DynamoDB.

    GET /                    the page (page.html, in the same zip); its script reads /api/zone-counts every 60 s
    GET /api/zone-counts     per zone: 12 values (null where nothing was counted), as_of, age_minutes, stale
    GET /api/slots?day=D     with SLOTS_TABLE set: the 96 slots of a UTC day and their states (slots.py)

No route starts an Athena query and no route writes. Any other method or path is answered 405 or 404 before a
DynamoDB client is made (README, Decisions).

Environment:
  PREFIX          name prefix, default "recipe"
  COUNTS_TABLE    default <PREFIX>-zone-counts (key zone_id, a number; sort key period_start, a string)
  ZONES           the zones on the chart, default "48,161,230"
  PERIOD          length of one period: "5m" (default) or a number of m, h or d ("1d" for a lake's daily counts)
  STALE_MINUTES   a zone whose newest counted request is older than this is stale, default 10
  SLOTS_TABLE     the slot table of streaming-ingestion/ext-source-poller; unset: no /api/slots route

Items of COUNTS_TABLE: count items (zone_id, period_start, requested, newest_event_time, and from the lake job
generation) and one `latest` item per zone (period_start = "latest", newest_event_time). "latest" sorts after every
date, so the one Query per zone that reads the window also reads it.
"""
from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
WINDOW = 12                      # periods on the chart
LATEST = "latest"                # sort key of each zone's latest item
CALLS: list[tuple[str, dict]] = []   # DynamoDB calls of the current request (operation, parameters), for the tests
_ddb = None


def settings() -> dict:
    prefix = os.environ.get("PREFIX") or "recipe"
    return {"counts_table": os.environ.get("COUNTS_TABLE") or f"{prefix}-zone-counts",
            "zones": [int(z) for z in (os.environ.get("ZONES") or "48,161,230").split(",") if z.strip()],
            "period": parse_period(os.environ.get("PERIOD") or "5m"),
            "stale_minutes": float(os.environ.get("STALE_MINUTES") or 10),
            "slots_table": os.environ.get("SLOTS_TABLE") or None}


def parse_period(text: str) -> timedelta:
    m = re.fullmatch(r"\s*(\d+)\s*([mhd])\s*", text)
    if not m:
        raise ValueError(f"PERIOD must be a number of m, h or d, such as 5m or 1d, not {text!r}")
    return timedelta(**{{"m": "minutes", "h": "hours", "d": "days"}[m.group(2)]: int(m.group(1))})


def ddb():
    """One DynamoDB client per execution environment, made on the first read (never for a refused request)."""
    global _ddb
    if _ddb is None:
        import time as _t
        t0 = _t.perf_counter()
        import boto3
        t1 = _t.perf_counter()
        _ddb = boto3.client("dynamodb")
        t2 = _t.perf_counter()
        print(json.dumps({"timing": "first-read client", "import_boto3_ms": round((t1 - t0) * 1000, 1),
                          "make_client_ms": round((t2 - t1) * 1000, 1)}))
        _ddb.meta.events.register("provide-client-params.dynamodb.*",
                                  lambda params, model, **_: CALLS.append((model.name, dict(params))))
    return _ddb


# ---- time

def utc(s: str) -> datetime:
    """A time string as an aware UTC datetime; one without an offset (Athena prints them so) is UTC."""
    t = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t.astimezone(timezone.utc)


def stamp(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def period_key(t: datetime, period: timedelta) -> str:
    """The sort key of the period that starts at t: a date for whole days, else the time to the second."""
    return t.strftime("%Y-%m-%d") if period % timedelta(days=1) == timedelta(0) else stamp(t)


def floor(t: datetime, period: timedelta) -> datetime:
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    return epoch + ((t - epoch) // period) * period


def window(now: datetime, period: timedelta) -> list[str]:
    """The 12 completed periods before the one that holds `now`, oldest first."""
    current = floor(now, period)
    return [period_key(current - (WINDOW - i) * period, period) for i in range(WINDOW)]


# ---- routes

def reply(status: int, body, content_type: str = "application/json") -> dict:
    text = body if isinstance(body, str) else json.dumps(body)
    return {"statusCode": status, "headers": {"content-type": content_type, "cache-control": "no-store"},
            "body": text}


def zone_counts(now: datetime, cfg: dict) -> dict:
    period, periods = cfg["period"], window(now, cfg["period"])
    wanted = set(periods)
    found = {}
    for zone in cfg["zones"]:
        items, start = [], None
        while True:                       # one Query; a second page only past 1 MB of items
            kw = dict(TableName=cfg["counts_table"], ConsistentRead=False,
                      KeyConditionExpression="zone_id = :z AND period_start >= :first",
                      ExpressionAttributeValues={":z": {"N": str(zone)}, ":first": {"S": periods[0]}})
            if start:
                kw["ExclusiveStartKey"] = start
            page = ddb().query(**kw)
            items += page["Items"]
            start = page.get("LastEvaluatedKey")
            if not start:
                break
        found[zone] = items
    has_latest = any(it["period_start"]["S"] == LATEST for items in found.values() for it in items)
    zones = []
    for zone in cfg["zones"]:
        values = {p: None for p in periods}
        latest, newest_in_window = None, None
        for it in found[zone]:
            key = it["period_start"]["S"]
            t = it.get("newest_event_time", {}).get("S")
            if key == LATEST:
                latest = t
                continue
            if key in wanted and "requested" in it:
                values[key] = int(it["requested"]["N"])
            if t and (newest_in_window is None or utc(t) > utc(newest_in_window)):
                newest_in_window = t
        # A table with latest items: a zone without one was never counted. A borrowed table without any: the
        # newest event time of the items read is the best there is (README, Decisions: latest item).
        as_of = latest if has_latest else newest_in_window
        zones.append(freshness({"zone_id": zone, "values": [values[p] for p in periods]}, as_of, now, cfg))
    times = [z["as_of"] for z in zones if z["as_of"]]
    data_as_of = max(times, key=utc) if times else None
    page = freshness({}, data_as_of, now, cfg)
    return {"read_at": stamp(now), "data_as_of": page["as_of"], "stale": page["stale"],
            "age_minutes": page["age_minutes"],
            "message": None if data_as_of else "no data; freshness unknown",
            "period_minutes": period / timedelta(minutes=1), "stale_minutes": cfg["stale_minutes"],
            "slots": cfg["slots_table"] is not None, "periods": periods, "zones": zones}


def freshness(entry: dict, as_of: str | None, now: datetime, cfg: dict) -> dict:
    if as_of is None:
        return entry | {"as_of": None, "age_minutes": None, "stale": True}
    age = now - utc(as_of)
    return entry | {"as_of": stamp(utc(as_of)), "age_minutes": int(age // timedelta(minutes=1)),
                    "stale": age > timedelta(minutes=cfg["stale_minutes"])}


def slots(day: str, now: datetime, cfg: dict) -> dict:
    from slots import slot_states
    items, start = [], None
    while True:                           # every page followed; 96 small items fit in one
        kw = dict(TableName=cfg["slots_table"], ConsistentRead=True,
                  KeyConditionExpression="slot_day = :d", ExpressionAttributeValues={":d": {"S": day}})
        if start:
            kw["ExclusiveStartKey"] = start
        page = ddb().query(**kw)
        items += [{k: next(iter(v.values())) for k, v in it.items()} for it in page["Items"]]
        start = page.get("LastEvaluatedKey")
        if not start:
            break
    return {"day": day, "read_time": stamp(now), "slots": slot_states(items, day, now)}


def is_date(text) -> bool:
    if not isinstance(text, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return False
    try:
        date.fromisoformat(text)
        return True
    except ValueError:
        return False


def handle(event: dict, now: datetime | None = None) -> dict:
    """One function URL request (payload format 2.0) at `now` (default: the clock)."""
    CALLS.clear()
    now = now or datetime.now(timezone.utc)
    cfg = settings()
    http = (event.get("requestContext") or {}).get("http") or {}
    method, path = http.get("method", "GET").upper(), event.get("rawPath") or http.get("path") or "/"
    routes = ["/", "/api/zone-counts"] + (["/api/slots"] if cfg["slots_table"] else [])
    if path not in routes:
        return reply(404, {"error": f"no route {path}"})
    if method != "GET":
        return reply(405, {"error": "this page only reads: use GET"})
    if path == "/":
        return reply(200, (HERE / "page.html").read_text(), "text/html; charset=utf-8")
    if path == "/api/zone-counts":
        return reply(200, zone_counts(now, cfg))
    day = (event.get("queryStringParameters") or {}).get("day")
    if not is_date(day):
        return reply(400, {"error": "day must be a UTC date, YYYY-MM-DD"})
    return reply(200, slots(day, now, cfg))


def lambda_handler(event, context=None):
    import time as _t
    t0 = _t.perf_counter()
    out = handle(event)
    print(json.dumps({"timing": "handle", "handle_ms": round((_t.perf_counter() - t0) * 1000, 1)}))
    print(json.dumps({"request_id": getattr(context, "aws_request_id", None), "path": event.get("rawPath"),
                      "status": out["statusCode"], "dynamodb_calls": len(CALLS)}))
    return out

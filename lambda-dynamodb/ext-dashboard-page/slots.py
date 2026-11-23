"""The state of every slot of a day, for the page's slot list (GET /api/slots?day=<UTC date>).

    from slots import slot_states, get_slots
    slot_states(items, "2026-11-22", now)       # 96 dicts in slot order
    status, body = get_slots({"day": "2026-11-22"}, ddb, "recipe-feed-slots", now)

A slot with an item has the item's state (`loaded`, `late`, `loading`, `failed`); one without an item is `missing`
when it ended more than 30 min before `now`, else `not due` (the partner publishes about 2 min after a slot ends,
so a slot inside its 30 min is not yet a gap). `record_total` is given for `loaded` and `late` only.

dashboard-page's route imports this file (lambda-dynamodb/ext-dashboard-page/slots.py is a copy: the two must stay
the same, tools/shared_helpers.toml); its role needs dynamodb:Query on the slot table. Only the standard library and a
boto3 client passed in.
"""

from datetime import date, datetime, timedelta, timezone

SLOT = timedelta(minutes=15)
MISSING_AFTER = timedelta(minutes=30)
ITEM_STATES = ("loaded", "late", "loading", "failed")


def _name(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%MZ")


def _parse(s: str) -> datetime:
    """A slot_start with or without seconds, with Z, an offset or no zone (read as UTC)."""
    t = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t.astimezone(timezone.utc)


def slot_states(items: list[dict], day: str | date, now: datetime) -> list[dict]:
    """The 96 slots of `day` (UTC), each with its state at `now` (an aware datetime). `items` are plain dicts
    (slot_start, slot_state, record_total ...); an item whose state is not one the poller writes counts as no item."""
    by_start = {_name(_parse(i["slot_start"])): i for i in items}
    start = datetime.combine(date.fromisoformat(str(day)), datetime.min.time(), tzinfo=timezone.utc)
    out = []
    for k in range(96):
        t = start + k * SLOT
        name = _name(t)
        item = by_start.get(name)
        if item and item.get("slot_state") in ITEM_STATES:
            entry = {"slot_start": name, "slot_state": str(item["slot_state"])}
            if item["slot_state"] in ("loaded", "late") and item.get("record_total") is not None:
                entry["record_total"] = int(item["record_total"])
        else:
            entry = {"slot_start": name, "slot_state": "missing" if now - (t + SLOT) > MISSING_AFTER else "not due"}
        out.append(entry)
    return out


def query_day(ddb, table: str, day: str) -> list[dict]:
    """The day's slot items with one Query (every page followed), types taken off."""
    found, kw = [], {"TableName": table, "KeyConditionExpression": "slot_day = :d",
                     "ExpressionAttributeValues": {":d": {"S": day}}}
    while True:
        page = ddb.query(**kw)
        found += [{k: next(iter(v.values())) for k, v in it.items()} for it in page["Items"]]
        if "LastEvaluatedKey" not in page:
            return found
        kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def get_slots(params: dict, ddb, table: str, now: datetime | None = None) -> tuple[int, dict]:
    """(status, body) of GET /api/slots: 400 when `day` is not a date (YYYY-MM-DD)."""
    now = now or datetime.now(timezone.utc)
    day = (params or {}).get("day", "")
    try:
        if len(day) != 10:
            raise ValueError(day)
        date.fromisoformat(day)
    except ValueError:
        return 400, {"error": f"day must be a UTC date like 2026-11-22, not {day!r}"}
    return 200, {"day": day, "read_time": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                 "slots": slot_states(query_day(ddb, table, day), day, now)}

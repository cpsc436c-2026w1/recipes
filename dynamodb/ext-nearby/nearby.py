#!/usr/bin/env python3
"""ext-nearby: drivers near a pickup point, and the nearest static points, in DynamoDB.

Each item is stored under its map cell (a geohash) and its type; a request reads its cell and the cells around it by
key, and the code computes the exact distances. Python's standard library and boto3 only, so it runs in CloudShell
as it is.

    python3 nearby.py tables                     create the two cell tables (kept when they are there); borrow <PREFIX>-drivers
    python3 nearby.py expect                     compute SIM01's check answers from SIM01 and SIM02 alone (no AWS)
    python3 nearby.py seed                       put SIM02's 60 drivers at their start positions
    python3 nearby.py replay [--speed 5]         SIM02's positions and SIM01's requests in time order, each request checked
    python3 nearby.py bench                      SIM01's 837 requests one at a time; p50, p95, p99 against 1,000 ms
    python3 nearby.py load [--points FILE]       store static points (default SIM04) under cells of a precision from their spacing
    python3 nearby.py nearest LAT LON TYPE N     the N static points nearest to a point
    python3 nearby.py nearby LAT LON CAR_TYPE    the drivers within 500 m of a point

Settings: PREFIX (default recipe), AWS_REGION (default ca-central-1), MAX_UNITS (default 100), MAX_RINGS (default 3).
The README of this folder explains each choice; a comment names its section (README, Decisions: ...).
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "sim" / "data"
sys.path.insert(0, str(HERE / "scripts"))
from tags import prefix, record, tags  # noqa: E402  (scripts/tags.py, a shared helper copied unchanged)

# ---- settings (README, Decisions and Assumptions) ------------------------------------------------------------------
PRECISION = 6                      # moving items: a precision-6 cell is about 611 m by 925 m here (Decisions: cells)
RADIUS_M = 500                     # the dispatcher's radius
MAX_ANSWER = 5                     # at most 5 drivers in an answer
POOL = 9                           # the point's cell and its 8 neighbours, sent at once
CAR_TYPES = ("standard", "xl")     # assumed (README, Assumptions)
AREA = {"lat": (40.70, 40.88), "lon": (-74.03, -73.90)}  # the service area, Manhattan; assumed (Assumptions)
EARTH_M = 6371008.8                # mean Earth radius, the great-circle distance's
SETTINGS_KEY = {"cell_type": {"S": "settings"}, "point_id": {"S": "precision"}}
TX_RETRIES = 3                     # retries from the stored driver after a refused condition (R82)
CONFLICT_ATTEMPTS = 5              # attempts after a TransactionConflict or a throttle inside a cancellation
# USD, ca-central-1 on demand, Standard table class: the course price sheet (DynamoDB file published 2026-11-11)
USD_PER_MILLION_WRITE_UNITS = 0.6875
USD_PER_MILLION_READ_UNITS = 0.1375
CAD_PER_USD = 1.45                 # the course's planning rate

ALPHABET = "0123456789bcdefghjkmnpqrstuvwxyz"


def region() -> str:
    return os.environ.get("AWS_REGION") or "ca-central-1"


def names() -> dict[str, str]:
    p = prefix()
    return {"cells": f"{p}-drivers-by-cell", "points": f"{p}-points-by-cell", "drivers": f"{p}-drivers"}


def max_rings() -> int:
    return int(os.environ.get("MAX_RINGS") or 3)


# ---- geohash cells (README, Decisions: cells) ----------------------------------------------------------------------
def encode(lat: float, lon: float, precision: int) -> str:
    """The geohash of a point: longitude and latitude bits interleaved, longitude first, five bits a character."""
    lat_r, lon_r = [-90.0, 90.0], [-180.0, 180.0]
    out, value, bits, even = [], 0, 0, True
    while len(out) < precision:
        r, x = (lon_r, lon) if even else (lat_r, lat)
        mid = (r[0] + r[1]) / 2
        if x >= mid:
            value, r[0] = value * 2 + 1, mid
        else:
            value, r[1] = value * 2, mid
        even, bits = not even, bits + 1
        if bits == 5:
            out.append(ALPHABET[value])
            value, bits = 0, 0
    return "".join(out)


def decode(cell: str) -> tuple[float, float, float, float]:
    """A cell's centre and its height and width in degrees: (lat, lon, height, width)."""
    lat_r, lon_r, even = [-90.0, 90.0], [-180.0, 180.0], True
    for ch in cell:
        v = ALPHABET.index(ch)
        for i in range(4, -1, -1):
            r = lon_r if even else lat_r
            mid = (r[0] + r[1]) / 2
            if (v >> i) & 1:
                r[0] = mid
            else:
                r[1] = mid
            even = not even
    return (lat_r[0] + lat_r[1]) / 2, (lon_r[0] + lon_r[1]) / 2, lat_r[1] - lat_r[0], lon_r[1] - lon_r[0]


def ring(cell: str, k: int) -> list[str]:
    """The cells of ring k around a cell: those of the points centre + (i x height, j x width), max(|i|,|j|) = k."""
    lat, lon, h, w = decode(cell)
    if k == 0:
        return [cell]
    return [encode(lat + i * h, lon + j * w, len(cell))
            for i in range(-k, k + 1) for j in range(-k, k + 1) if max(abs(i), abs(j)) == k]


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """The great-circle distance (haversine), in metres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_M * math.asin(math.sqrt(h))


def shorter_side_m(cell: str, k: int = 1) -> float:
    """The shorter side of a cell, in metres, for a block of rings 0 to k around it. The width is taken at the block's
    edge farthest from the equator, where a cell is narrowest, so the covered distance is never overstated."""
    lat, lon, h, w = decode(cell)
    height = math.radians(h) * EARTH_M
    edge = min(abs(lat) + (k + 0.5) * h, 90.0)
    width = math.radians(w) * EARTH_M * math.cos(math.radians(edge))
    return min(height, width)


def in_area(lat: float, lon: float) -> bool:
    return AREA["lat"][0] <= lat <= AREA["lat"][1] and AREA["lon"][0] <= lon <= AREA["lon"][1]


# ---- times ---------------------------------------------------------------------------------------------------------
def parse_time(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def iso(t: datetime | str) -> str:
    """One format for every stored time, so that DynamoDB's string comparison is the time order."""
    if isinstance(t, str):
        t = parse_time(t)
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


# ---- the client and its units --------------------------------------------------------------------------------------
class Units:
    """Consumed request units, per kind of call, from ReturnConsumedCapacity; thread-safe."""

    def __init__(self):
        self.lock = threading.Lock()
        self.read: dict[str, float] = {}
        self.write: dict[str, float] = {}

    def add(self, kind: str, reply: dict, write: bool = False) -> None:
        caps = reply.get("ConsumedCapacity") or []
        caps = caps if isinstance(caps, list) else [caps]
        units = sum(c.get("CapacityUnits", 0) or 0 for c in caps)
        with self.lock:
            book = self.write if write else self.read
            book[kind] = book.get(kind, 0) + units

    def cost(self) -> dict:
        r, w = sum(self.read.values()), sum(self.write.values())
        usd = r * USD_PER_MILLION_READ_UNITS / 1e6 + w * USD_PER_MILLION_WRITE_UNITS / 1e6
        return {"read_units": round(r, 1), "write_units": round(w, 1), "usd": round(usd, 6),
                "cad": round(usd * CAD_PER_USD, 6), "read_by_kind": self.read, "write_by_kind": self.write}


UNITS = Units()


def client(session=None):
    """A DynamoDB client with adaptive retries (10 attempts): a replay faster than real time meets the MAX_UNITS
    ceiling, and throttled calls are slowed down and sent again (README, Decisions: retries)."""
    import boto3
    from botocore.config import Config

    s = session or boto3.session.Session()
    return s.client("dynamodb", region_name=region(),
                    config=Config(retries={"mode": "adaptive", "max_attempts": 10}, max_pool_connections=20))


_POOL: ThreadPoolExecutor | None = None


def pool() -> ThreadPoolExecutor:
    global _POOL
    if _POOL is None:
        _POOL = ThreadPoolExecutor(max_workers=POOL)
    return _POOL


# ---- tables (README, Decisions: tables made by nearby.py) ----------------------------------------------------------
def table_tags() -> list[dict]:
    """The course tags (scripts/tags.py) and the parent recipe's two, purpose=recipe and prefix=$PREFIX (R33)."""
    values = {t["Key"]: t["Value"] for t in tags("dynamodb")}
    values.setdefault("purpose", "recipe")
    values["prefix"] = prefix()
    return [{"Key": k, "Value": v} for k, v in values.items()]


def table_status(c, name: str) -> str | None:
    """The table's status, or None when DynamoDB answers ResourceNotFoundException. Any other error is raised."""
    try:
        return c.describe_table(TableName=name)["Table"]["TableStatus"]
    except c.exceptions.ResourceNotFoundException:
        return None


def ensure_table(c, name: str, sort_key: str, script: str = "ext-nearby/nearby.py tables") -> str:
    """Create an on-demand table with partition key cell_type and the given sort key, or keep the one that is there.
    A table an earlier clean-up is still deleting is waited out first. Returns created or kept."""
    need_sdk(c)
    status = table_status(c, name)
    if status == "DELETING":
        print(f"(table {name} is still being deleted by an earlier clean-up: waiting until it is gone)")
        c.get_waiter("table_not_exists").wait(TableName=name)
        status = None
    if status:
        print(f"(table {name} is already there: kept as it is)")
        c.get_waiter("table_exists").wait(TableName=name)
        return "kept"
    units = int(os.environ.get("MAX_UNITS") or 100)
    c.create_table(
        TableName=name, BillingMode="PAY_PER_REQUEST",
        OnDemandThroughput={"MaxReadRequestUnits": units, "MaxWriteRequestUnits": units},
        KeySchema=[{"AttributeName": "cell_type", "KeyType": "HASH"}, {"AttributeName": sort_key, "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "cell_type", "AttributeType": "S"},
                              {"AttributeName": sort_key, "AttributeType": "S"}],
        Tags=table_tags())
    c.get_waiter("table_exists").wait(TableName=name)
    record("dynamodb-table", name, "created", script=script, clean="ext-nearby/clean_up.py --yes")
    print(f"created table {name}: partition key cell_type, sort key {sort_key}, on demand, at most {units} units a second")
    return "created"


class MissingTable(Exception):
    pass


class OldSDK(Exception):
    pass


def sdk_has_ceiling(c) -> bool:
    """Whether this boto3 knows CreateTable's OnDemandThroughput (the MAX_UNITS ceiling). boto3 1.34.46 does not (the
    live run of 2026-11-23), and refuses the create on the client with ParamValidationError; 1.44.2 does."""
    return "OnDemandThroughput" in c.meta.service_model.operation_model("CreateTable").input_shape.members


def need_sdk(c) -> None:
    """Stop before any call to AWS and any ledger line when the SDK cannot set the ceiling (README, Run it)."""
    if not sdk_has_ceiling(c):
        import boto3

        raise OldSDK(f"FAILED: this boto3 ({boto3.__version__}) does not know OnDemandThroughput, the ceiling of "
                     f"MAX_UNITS request units a second that every table here gets. Nothing was created. Run it with "
                     f"the project's boto3 (uv run bash ext-nearby/run.sh) or a newer boto3, then run again")


def borrow_drivers(c) -> None:
    """<PREFIX>-drivers is ext-riders-and-drivers' table: it must be there; it is recorded borrowed, never deleted."""
    name = names()["drivers"]
    if table_status(c, name) is None:
        raise MissingTable(f"Table {name} not found. Run ext-riders-and-drivers first with KEEP=1, for example: "
                           f"KEEP=1 bash main/run.sh && KEEP=1 bash ext-riders-and-drivers/run.sh")
    record("dynamodb-table", name, "borrowed", script="ext-nearby/nearby.py tables",
           clean="ext-nearby/clean_up.py --yes removes the attributes cell, car_type, lat, lon, position_time")
    print(f"(table {name} is there: borrowed; its drivers get the attributes cell, car_type, lat, lon, position_time)")


def tables(c) -> None:
    need_sdk(c)
    borrow_drivers(c)
    ensure_table(c, names()["cells"], "driver_id")
    ensure_table(c, names()["points"], "point_id")


# ---- positions (R80, R81, R82; README, Decisions: one transaction) -------------------------------------------------
NAMES = {"#cell": "cell", "#ct": "car_type", "#lat": "lat", "#lon": "lon", "#pt": "position_time"}


def _num(x: float) -> dict:
    return {"N": repr(round(float(x), 7))}


def stored_driver(item: dict | None) -> dict | None:
    """The fields of a <PREFIX>-drivers item this extension reads, or None when the driver has no position yet."""
    if not item or "cell" not in item:
        return None
    return {"cell": item["cell"]["S"], "car_type": item["car_type"]["S"],
            "lat": float(item["lat"]["N"]), "lon": float(item["lon"]["N"]), "position_time": item["position_time"]["S"]}


def read_driver(c, driver_id: str) -> dict | None:
    """The stored driver, from a strongly consistent GetItem on <PREFIX>-drivers."""
    reply = c.get_item(TableName=names()["drivers"], Key={"driver_id": {"S": driver_id}}, ConsistentRead=True,
                       ReturnConsumedCapacity="TOTAL")
    UNITS.add("get driver", reply)
    return stored_driver(reply.get("Item"))


_NOT_READ = object()


def put_position(c, driver_id: str, car_type: str, lat: float, lon: float, when: str, *, seen=_NOT_READ,
                 sleep=time.sleep) -> dict:
    """Store a driver's position under its cell and car type in one transaction (R80, R81). `seen` is the stored
    driver as read before (None: no position stored); by default it is read now.

    Returns {"result": "stored" | "older, kept", "cell", "moved", "attempts": [...]}; each attempt says whether it
    committed and, when its condition was refused, the cell it then read."""
    when = iso(when)
    cell = encode(lat, lon, PRECISION)
    n = names()
    if seen is _NOT_READ:
        seen = read_driver(c, driver_id)
    attempts: list[dict] = []
    refused = 0
    conflicts = 0
    while True:
        old_key = f"{seen['cell']}#{seen['car_type']}" if seen else None
        new_key = f"{cell}#{car_type}"
        if seen:
            same = "#cell = :seen_cell AND #ct = :seen_ct"
        else:
            same = "attribute_not_exists(#cell) AND attribute_not_exists(#ct)"
        values = {":cell": {"S": cell}, ":ct": {"S": car_type}, ":lat": _num(lat), ":lon": _num(lon),
                  ":pt": {"S": when}}
        if seen:
            values |= {":seen_cell": {"S": seen["cell"]}, ":seen_ct": {"S": seen["car_type"]}}
        items = [
            {"Update": {
                "TableName": n["drivers"], "Key": {"driver_id": {"S": driver_id}},
                "UpdateExpression": "SET #cell = :cell, #ct = :ct, #lat = :lat, #lon = :lon, #pt = :pt",
                "ConditionExpression": f"(attribute_not_exists(#pt) OR #pt < :pt) AND ({same})",
                "ExpressionAttributeNames": NAMES, "ExpressionAttributeValues": values,
                "ReturnValuesOnConditionCheckFailure": "ALL_OLD"}},
            {"Put": {"TableName": n["cells"], "Item": {
                "cell_type": {"S": new_key}, "driver_id": {"S": driver_id}, "lat": _num(lat), "lon": _num(lon),
                "position_time": {"S": when}}}},
        ]
        if old_key and old_key != new_key:
            items.append({"Delete": {"TableName": n["cells"],
                                     "Key": {"cell_type": {"S": old_key}, "driver_id": {"S": driver_id}}}})
        try:
            reply = c.transact_write_items(TransactItems=items, ReturnConsumedCapacity="TOTAL")
            UNITS.add("position transaction", reply, write=True)
            attempts.append({"committed": True, "items": len(items)})
            return {"result": "stored", "cell": cell, "moved": bool(old_key and old_key != new_key),
                    "attempts": attempts}
        except c.exceptions.TransactionCanceledException as e:
            # a cancelled transaction is billed too: 2 write units per item (README, Cost); not in the reply
            UNITS.write["cancelled transaction (estimated)"] = UNITS.write.get(
                "cancelled transaction (estimated)", 0) + 2 * len(items)
            reasons = e.response.get("CancellationReasons") or []
            codes = [r.get("Code") for r in reasons]
            if codes and codes[0] == "ConditionalCheckFailed":
                # the stored driver came back with the refusal (ALL_OLD); read it if this DynamoDB did not send it
                stored = stored_driver(reasons[0].get("Item")) if reasons[0].get("Item") else read_driver(c, driver_id)
                attempts.append({"committed": False, "read_cell": stored["cell"] if stored else None})
                if stored and stored["position_time"] >= when:
                    return {"result": "older, kept", "cell": stored["cell"], "moved": False, "attempts": attempts}
                refused += 1
                if refused > TX_RETRIES:
                    raise RuntimeError(f"driver {driver_id}: refused {refused} times, gave up after {TX_RETRIES} "
                                       f"retries") from e
                seen = stored
                continue
            # TransactionConflict or a throttle inside a cancellation: the SDKs do not retry a cancelled
            # transaction (developer guide, transaction-apis), so this loop does, a few times with a pause
            if all(code in ("None", "TransactionConflict", "ThrottlingError", "ProvisionedThroughputExceeded")
                   for code in codes) and conflicts + 1 < CONFLICT_ATTEMPTS:
                conflicts += 1
                attempts.append({"committed": False, "conflict": codes})
                sleep(0.05 * 2 ** conflicts + random.random() * 0.05)
                continue
            raise


# ---- reads (R90 to R94) --------------------------------------------------------------------------------------------
def query_cell(c, table: str, key: str, consistent: bool) -> list[dict]:
    """Every item under one partition key, following LastEvaluatedKey (a Query returns at most 1 MB a page)."""
    items, start = [], None
    while True:
        kw = {"TableName": table, "KeyConditionExpression": "cell_type = :k",
              "ExpressionAttributeValues": {":k": {"S": key}}, "ConsistentRead": consistent,
              "ReturnConsumedCapacity": "TOTAL"}
        if start:
            kw["ExclusiveStartKey"] = start
        reply = c.query(**kw)
        UNITS.add("query" + (" strong" if consistent else ""), reply)
        items += reply.get("Items", [])
        start = reply.get("LastEvaluatedKey")
        if not start:
            return items


def query_cells(c, table: str, keys: list[str], consistent: bool) -> list[tuple[str, dict]]:
    """One Query per key, sent at once from the pool; (key, item) for every item read."""
    results = list(pool().map(lambda k: query_cell(c, table, k, consistent), keys))
    return [(k, item) for k, found in zip(keys, results) for item in found]


def answer_from(items, lat: float, lon: float, id_field: str) -> list[dict]:
    """Distances in code, nearest first; ties by id, so that two answers from the same items are the same."""
    out = []
    for key, it in items:
        plat, plon = float(it["lat"]["N"]), float(it["lon"]["N"])
        out.append({id_field: it[id_field]["S"], "distance": distance_m(lat, lon, plat, plon),
                    "cell": key.split("#")[0], "lat": plat, "lon": plon})
    out.sort(key=lambda d: (d["distance"], d[id_field]))
    return out


def shown(rows: list[dict], id_field: str) -> list[dict]:
    return [{id_field: r[id_field], "distance_m": round(r["distance"]), "cell": r["cell"]} for r in rows]


def nearby(c, lat: float, lon: float, car_type: str, consistent: bool = False) -> dict:
    """The drivers of a car type within 500 m of a point, nearest first, at most 5 (R90). The service-area and
    car-type checks come before any read (R91, R92); an empty answer names the radius searched (R93)."""
    if not in_area(lat, lon):
        return {"error": "outside coverage"}
    if car_type not in CAR_TYPES:
        return {"error": "unknown car type"}
    centre = encode(lat, lon, PRECISION)
    cells = ring(centre, 0) + ring(centre, 1)
    items = query_cells(c, names()["cells"], [f"{x}#{car_type}" for x in cells], consistent)
    near = [d for d in answer_from(items, lat, lon, "driver_id") if d["distance"] <= RADIUS_M][:MAX_ANSWER]
    return {"drivers": shown(near, "driver_id"), "radius_m": RADIUS_M, "cells": cells, "queries": len(cells),
            "pool": POOL}


def check_nearby(c, lat: float, lon: float, car_type: str) -> list[dict]:
    """The answer computed from every stored driver of that car type: a strongly consistent Scan, paginated. Used by
    the check only, never by a request (R94)."""
    items, start = [], None
    while True:
        kw = {"TableName": names()["cells"], "ConsistentRead": True, "ReturnConsumedCapacity": "TOTAL"}
        if start:
            kw["ExclusiveStartKey"] = start
        reply = c.scan(**kw)
        UNITS.add("check scan", reply)
        items += [(it["cell_type"]["S"], it) for it in reply.get("Items", [])
                  if it["cell_type"]["S"].endswith(f"#{car_type}")]
        start = reply.get("LastEvaluatedKey")
        if not start:
            break
    near = [d for d in answer_from(items, lat, lon, "driver_id") if d["distance"] <= RADIUS_M][:MAX_ANSWER]
    return shown(near, "driver_id")


# ---- static points (R100, R101, R102) ------------------------------------------------------------------------------
def read_points(path: Path) -> list[dict]:
    with open(path) as f:
        return [{"point_id": r["point_id"], "type": r["type"], "lat": float(r["lat"]), "lon": float(r["lon"])}
                for r in csv.DictReader(f)]


def median(xs: list[float]) -> float:
    s = sorted(xs)
    m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2


def choose_precision(points: list[dict]) -> dict:
    """The finest precision whose cell's shorter side is at least the median distance from each point to its
    nearest other point (R100). The side is taken at the points' mean position."""
    nearest = [min(distance_m(p["lat"], p["lon"], q["lat"], q["lon"]) for q in points if q is not p) for p in points]
    med = median(nearest)
    lat = sum(p["lat"] for p in points) / len(points)
    lon = sum(p["lon"] for p in points) / len(points)
    for precision in range(12, 0, -1):
        side = shorter_side_m(encode(lat, lon, precision))
        if side >= med:
            return {"precision": precision, "median_m": med, "side_m": side}
    return {"precision": 1, "median_m": med, "side_m": shorter_side_m(encode(lat, lon, 1))}


def batch_put(c, table: str, items: list[dict], sleep=time.sleep) -> None:
    """BatchWriteItem in calls of 25, sending unprocessed items again after 1, 2 and 4 s, at most 4 calls a batch."""
    for i in range(0, len(items), 25):
        req = {table: [{"PutRequest": {"Item": it}} for it in items[i:i + 25]]}
        for call in range(4):
            reply = c.batch_write_item(RequestItems=req, ReturnConsumedCapacity="TOTAL")
            UNITS.add("load", reply, write=True)
            req = {t: r for t, r in (reply.get("UnprocessedItems") or {}).items() if r}
            if not req:
                break
            if call == 3:
                raise RuntimeError("DynamoDB still returned unprocessed items after 4 calls; the load is not complete")
            sleep(2 ** call)


def load(c, points: list[dict]) -> dict:
    """Store static points under `<cell>#<type>` in <PREFIX>-points-by-cell, and the precision in its settings item."""
    ensure_table(c, names()["points"], "point_id", script="ext-nearby/nearby.py load")
    chosen = choose_precision(points)
    p = chosen["precision"]
    items = [{"cell_type": {"S": f"{encode(x['lat'], x['lon'], p)}#{x['type']}"}, "point_id": {"S": x["point_id"]},
              "lat": _num(x["lat"]), "lon": _num(x["lon"])} for x in points]
    settings = SETTINGS_KEY | {"precision": {"N": str(p)}, "median_m": {"N": str(round(chosen["median_m"]))}}
    batch_put(c, names()["points"], [settings] + items)
    stored = {x["point_id"]: f"{encode(x['lat'], x['lon'], p)}#{x['type']}" for x in points}
    return chosen | {"stored": stored}


def stored_precision(c) -> int:
    reply = c.get_item(TableName=names()["points"], Key=SETTINGS_KEY, ReturnConsumedCapacity="TOTAL")
    UNITS.add("settings", reply)
    if "Item" not in reply:
        raise RuntimeError("no settings item: run `nearby.py load` first")
    return int(reply["Item"]["precision"]["N"])


def nearest(c, lat: float, lon: float, type_: str, n: int, rings: int | None = None, consistent: bool = False) -> dict:
    """The n static points nearest to a point (R101): rings 0 and 1 at once, then ring 2, 3 ... one ring at a time,
    until n points lie within the covered distance (k x the shorter side after ring k). After MAX_RINGS rings it
    answers what lies within the covered distance, with complete false (R102)."""
    rings = rings or max_rings()
    precision = stored_precision(c)
    centre = encode(lat, lon, precision)
    table = names()["points"]
    read: list = []
    queries = 0
    for k in range(1, rings + 1):
        cells = (ring(centre, 0) + ring(centre, 1)) if k == 1 else ring(centre, k)
        read += query_cells(c, table, [f"{x}#{type_}" for x in cells], consistent)
        queries += len(cells)
        covered = k * shorter_side_m(centre, k)
        rows = answer_from(read, lat, lon, "point_id")
        certain = [r for r in rows if r["distance"] <= covered]
        if len(certain) >= n or k == rings:
            return {"points": shown(certain[:n], "point_id"), "complete": len(certain) >= n,
                    "covered_m": round(covered), "rings": k, "queries": queries, "precision": precision,
                    "read": shown(rows, "point_id")}
    raise AssertionError("unreachable")


# ---- simulated data, replay and check (R94) ------------------------------------------------------------------------
def read_csv(path: Path) -> list[dict]:
    with open(path) as f:
        return list(csv.DictReader(f))


def sim_drivers(data: Path = DATA) -> list[dict]:
    return read_csv(data / "SIM02" / "drivers.csv")


def sim_positions(data: Path = DATA) -> list[dict]:
    return read_csv(data / "SIM02" / "positions.csv")


def sim_requests(data: Path = DATA) -> list[dict]:
    return read_csv(data / "SIM01" / "requests.csv")


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def sim_answer(data: Path = DATA) -> dict:
    """SIM02's counts, computed from its _truth/ files (each position as it happened, one line per injected fault)
    and the start positions; the received times of positions.csv decide which late position is refused. A late
    position that arrives after a newer one of its driver is refused ("older, kept"); when the next one is late too,
    the earlier one is still the newest when it arrives, and is stored."""
    events = _jsonl(data / "SIM02" / "_truth" / "events.jsonl")
    faults = _jsonl(data / "SIM02" / "_truth" / "faults.jsonl")
    drivers = sim_drivers(data)
    cell = {d["driver_id"]: encode(float(d["lat"]), float(d["lon"]), PRECISION) for d in drivers}
    changes = 0
    for e in sorted(events, key=lambda e: (e["driver_id"], e["position_time"])):
        now = encode(float(e["lat"]), float(e["lon"]), PRECISION)
        changes += now != cell[e["driver_id"]]
        cell[e["driver_id"]] = now
    newest, older_kept = {}, 0
    for p in sorted(sim_positions(data), key=lambda p: (p["received_time"], p["delivery_id"])):
        if p["driver_id"] in newest and newest[p["driver_id"]] >= p["position_time"]:
            older_kept += 1
        else:
            newest[p["driver_id"]] = p["position_time"]
    return {"drivers": len(drivers), "positions": len(events), "late": sum(f["fault"] == "late" for f in faults),
            "older_kept": older_kept, "cell_changes": changes, "cell_change_share": round(changes / len(events), 4)}


def timeline(data: Path = DATA) -> list[tuple]:
    """SIM02's positions in the order they arrive (received_time) and SIM01's requests at their event_time, one list.
    At an equal time a position comes first: it has arrived when the request is read."""
    events = [(iso(p["received_time"]), 0, "position", p) for p in sim_positions(data)]
    events += [(iso(r["event_time"]), 1, "request", r) for r in sim_requests(data)]
    return sorted(events, key=lambda e: (e[0], e[1], e[3].get("delivery_id") or e[3].get("request_id")))


def expect(data: Path = DATA) -> list[dict]:
    """The check answers of SIM01, from SIM01 and SIM02 alone, in memory, with no cells: for each request, every
    driver's newest position that has arrived, all drivers of the car type within 500 m, nearest first, at most 5."""
    state = {d["driver_id"]: (iso(d["start_time"]), d["car_type"], float(d["lat"]), float(d["lon"]))
             for d in sim_drivers(data)}
    out = []
    for _, _, kind, e in timeline(data):
        if kind == "position":
            t = iso(e["position_time"])
            if t > state[e["driver_id"]][0]:
                state[e["driver_id"]] = (t, e["car_type"], float(e["lat"]), float(e["lon"]))
            continue
        lat, lon = float(e["lat"]), float(e["lon"])
        rows = sorted(((distance_m(lat, lon, s[2], s[3]), d) for d, s in state.items() if s[1] == e["car_type"]))
        near = [(d, round(m)) for m, d in rows if m <= RADIUS_M][:MAX_ANSWER]
        out.append({"request_id": e["request_id"], "car_type": e["car_type"],
                    "drivers": [{"driver_id": d, "distance_m": m} for d, m in near], "radius_m": RADIUS_M})
    return out


def write_expected(data: Path = DATA) -> Path:
    path = data / "SIM01" / "nearby_expected.jsonl"
    rows = expect(data)
    path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))
    print(f"wrote {len(rows)} expected answers to {path.relative_to(HERE)}")
    return path


def seed(c, data: Path = DATA) -> int:
    """Put SIM02's drivers at their start positions: one transaction of 2 items each."""
    drivers = sim_drivers(data)
    for d in drivers:
        put_position(c, d["driver_id"], d["car_type"], float(d["lat"]), float(d["lon"]), d["start_time"])
    print(f"seeded {len(drivers)} drivers")
    return len(drivers)


def ids(answer: list[dict]) -> list[tuple]:
    return [(d["driver_id"], d["distance_m"]) for d in answer]


def replay(c, data: Path = DATA, speed: float = 0, *, sleep=time.sleep, wall=time.monotonic, log=print) -> dict:
    """SIM02's positions and SIM01's requests in time order, `speed` times faster than real time (0: no waiting).
    Each write returns before the next event, so no write is in flight at a request. Each request runs `nearby` with
    strongly consistent Queries and is compared with the Scan check and with nearby_expected.jsonl."""
    expected = {}
    path = data / "SIM01" / "nearby_expected.jsonl"
    if path.exists():
        expected = {r["request_id"]: r for r in map(json.loads, path.read_text().splitlines())}
    events = timeline(data)
    t0, w0 = parse_time(events[0][0]), wall()
    s = {"positions": 0, "stored": 0, "older_kept": 0, "moves": 0, "retries": 0, "requests": 0,
         "equal_check": 0, "equal_expected": 0, "differ": []}
    for n, (t, _, kind, e) in enumerate(events):
        if speed:
            due = (parse_time(t) - t0).total_seconds() / speed
            ahead = due - (wall() - w0)
            if ahead > 0:
                sleep(ahead)
        if kind == "position":
            r = put_position(c, e["driver_id"], e["car_type"], float(e["lat"]), float(e["lon"]), e["position_time"])
            s["positions"] += 1
            s["stored" if r["result"] == "stored" else "older_kept"] += 1
            s["moves"] += r["moved"]
            s["retries"] += sum(1 for a in r["attempts"] if not a["committed"] and "read_cell" in a
                                and r["result"] == "stored")
            continue
        lat, lon = float(e["lat"]), float(e["lon"])
        got = nearby(c, lat, lon, e["car_type"], consistent=True)
        check = check_nearby(c, lat, lon, e["car_type"])
        s["requests"] += 1
        same_check = ids(got["drivers"]) == ids(check)
        exp = expected.get(e["request_id"])
        same_exp = exp is not None and ids(got["drivers"]) == ids(exp["drivers"])
        s["equal_check"] += same_check
        s["equal_expected"] += same_exp
        if not (same_check and same_exp):
            s["differ"].append(e["request_id"])
        if log and s["requests"] % 100 == 0:
            log(f"  {s['requests']} requests, {s['positions']} positions ...")
    s["units"] = UNITS.cost()
    return s


def quantile_cont(xs: list[float], q: float) -> float:
    """The continuous quantile, as DuckDB's quantile_cont: linear interpolation between the two nearest ranks."""
    s = sorted(xs)
    pos = q * (len(s) - 1)
    lo = math.floor(pos)
    hi = min(lo + 1, len(s) - 1)
    if s[hi] == math.inf:
        return math.inf if pos > lo or s[lo] == math.inf else s[lo]
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def bench_summary(latencies_ms: list[float], failures: int = 0) -> dict:
    """p50, p95 and p99 over the requests; a failure counts as over 1 s (infinitely long); pass when p99 < 1,000 ms."""
    xs = list(latencies_ms) + [math.inf] * failures

    def q(p: float):
        v = quantile_cont(xs, p)
        return round(v, 1) if v != math.inf else "inf"

    return {"requests": len(xs), "failures": failures, "p50_ms": q(0.5), "p95_ms": q(0.95), "p99_ms": q(0.99),
            "slo_ms": 1000, "pass": quantile_cont(xs, 0.99) < 1000}


BENCH_RATE = 15   # requests a second: 837 in about a minute, 67.5 read units a second, under MAX_UNITS (README, Decisions: the SLO)


def bench(c, data: Path = DATA, clock=time.perf_counter, rate: float = BENCH_RATE, sleep=time.sleep,
          wall=time.monotonic) -> dict:
    """SIM01's requests one at a time, the next when the answer has arrived and not faster than `rate` a second (0:
    no pacing), so that the bench stays under the table's MAX_UNITS ceiling and no throttle sits inside the p99.
    Eventually consistent, as the dispatcher reads. The clock runs from the call of nearby() to its return."""
    lat_ms, failures = [], 0
    start = wall()
    for i, r in enumerate(sim_requests(data)):
        if rate:
            ahead = start + i / rate - wall()
            if ahead > 0:
                sleep(ahead)
        t = clock()
        try:
            nearby(c, float(r["lat"]), float(r["lon"]), r["car_type"])
            lat_ms.append((clock() - t) * 1000)
        except Exception as e:  # after botocore's retries: a failure, and over 1 s
            failures += 1
            print(f"request {r['request_id']} failed: {type(e).__name__}", file=sys.stderr)
    return bench_summary(lat_ms, failures) | {"rate_per_s": rate}


# ---- command line --------------------------------------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("tables")
    sub.add_parser("expect")
    sub.add_parser("seed")
    r = sub.add_parser("replay")
    r.add_argument("--speed", type=float, default=5)
    sub.add_parser("bench")
    lp = sub.add_parser("load")
    lp.add_argument("--points", default=str(DATA / "SIM04" / "points.csv"))
    for name, extra in (("nearest", "TYPE N"), ("nearby", "CAR_TYPE")):
        p = sub.add_parser(name)
        p.add_argument("lat", type=float)
        p.add_argument("lon", type=float)
        p.add_argument("type")
        if name == "nearest":
            p.add_argument("n", type=int)
    a = ap.parse_args(argv)
    if a.cmd == "expect":
        write_expected()
        return 0
    c = client()
    try:
        if a.cmd == "tables":
            tables(c)
        elif a.cmd == "seed":
            seed(c)
        elif a.cmd == "replay":
            s = replay(c, speed=a.speed)
            print(json.dumps(s, indent=2))
            if s["differ"] or s["equal_check"] != s["requests"]:
                print(f"FAILED: {len(s['differ'])} answers differ from the check", file=sys.stderr)
                return 1
            print(f"{s['equal_expected']} of {s['requests']} answers equal nearby_expected.jsonl and the Scan check")
        elif a.cmd == "bench":
            s = bench(c)
            print(json.dumps(s, indent=2))
            # the units go out before the verdict's return, so the bench's read units are in the capture too
            print(json.dumps({"units": UNITS.cost()}))
            print(("PASS" if s["pass"] else "FAILED") + f": p50 {s['p50_ms']} ms, p95 {s['p95_ms']} ms, p99 "
                  f"{s['p99_ms']} ms against 1,000 ms over {s['requests']} requests")
            return 0 if s["pass"] else 1
        elif a.cmd == "load":
            print(json.dumps(load(c, read_points(Path(a.points))), indent=2))
        elif a.cmd == "nearest":
            ans = nearest(c, a.lat, a.lon, a.type, a.n)
            ans.pop("read")
            print(json.dumps(ans, indent=2))
        elif a.cmd == "nearby":
            print(json.dumps(nearby(c, a.lat, a.lon, a.type), indent=2))
        print(json.dumps({"units": UNITS.cost()}))
    except (MissingTable, OldSDK) as e:
        print(e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

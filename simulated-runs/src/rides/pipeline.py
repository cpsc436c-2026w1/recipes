"""A small stand-in for the team's pipeline, so the whole run has something to check: replace it with yours.

    handle(deliveries)        each delivery validated, its event stored once in DynamoDB table <PREFIX>-sim-events
                              (a conditional write on event_id, so a repeat or a second send changes nothing)
    stored_events()           every stored event
    requested_per_zone(rows)  the table "trips requested per zone", in DuckDB SQL
    fare_by_trip(rows)        the fare of each trip's latest completion, in DuckDB SQL

Offline, the table is in moto and `handle` is what `sim/replay.py --to local` calls. A team points the replayer at
its own stream, queue, URL or bucket instead and reads its own tables in the step code.
"""
from __future__ import annotations

import json
import os
import tempfile
from decimal import Decimal
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

from rides.events import DELIVERY, as_event

REGION = "ca-central-1"


def table_name() -> str:
    return os.environ.get("PIPELINE_TABLE") or f"{os.environ.get('PREFIX') or 'recipe'}-sim-events"


def _table():
    ddb = boto3.resource("dynamodb", region_name=REGION)
    table = ddb.Table(table_name())
    try:
        table.load()
    except ClientError as err:
        if err.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
        table = ddb.create_table(TableName=table_name(), BillingMode="PAY_PER_REQUEST",
                                 KeySchema=[{"AttributeName": "event_id", "KeyType": "HASH"}],
                                 AttributeDefinitions=[{"AttributeName": "event_id", "AttributeType": "S"}])
        table.wait_until_exists()
    return table


def handle(deliveries: list[dict]) -> dict:
    """Store each delivery's event once; returns how many were new and how many were repeats."""
    table, new, repeats = _table(), 0, 0
    for d in deliveries:
        event = as_event(DELIVERY.validate_python(d)).model_dump(mode="json")
        try:
            table.put_item(Item=json.loads(json.dumps(event), parse_float=Decimal),
                           ConditionExpression="attribute_not_exists(event_id)")
            new += 1
        except ClientError as err:
            if err.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            repeats += 1
    return {"new": new, "repeats": repeats}


def _number(d: Decimal):
    """DynamoDB numbers come back as Decimal: whole ones as int (a zone id), the others as float (a fare)."""
    return int(d) if d == d.to_integral_value() else float(d)


def stored_events() -> list[dict]:
    table, kw, rows = _table(), {}, []
    while True:
        page = table.scan(**kw)
        rows += [json.loads(json.dumps(i, default=_number)) for i in page["Items"]]
        if "LastEvaluatedKey" not in page:
            return rows
        kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _sql(rows: list[dict], query: str) -> list[tuple]:
    import duckdb
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "events.jsonl"
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))
        con = duckdb.connect()
        con.execute(f"CREATE VIEW events AS SELECT * FROM read_json_auto('{path}', union_by_name = true)")
        return con.execute(query).fetchall()


def requested_per_zone(rows: list[dict]) -> dict[int, int]:
    """Trips requested per pickup zone: one per trip, from its latest trip_requested."""
    return dict(_sql(rows, """
        SELECT pickup_zone_id, count(DISTINCT trip_id) FROM events
        WHERE event_type = 'trip_requested' GROUP BY pickup_zone_id ORDER BY pickup_zone_id"""))


def fare_by_trip(rows: list[dict]) -> dict[str, float]:
    """The fare of each trip's latest trip_completed (a correction has a later event_time)."""
    return dict(_sql(rows, """
        SELECT trip_id, arg_max(fare, (event_time, event_id)) FROM events
        WHERE event_type = 'trip_completed' GROUP BY trip_id"""))

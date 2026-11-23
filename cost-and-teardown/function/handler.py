"""The demonstration's counting function: trip requests per zone, each request counted once.

Kinesis gives a batch of deliveries; for each `trip_requested` one transaction puts the event's own item (only if
it is not there: a repeat delivery is cancelled) and adds 1 to its zone's counter. Accepts and completions are left
aside. Every transaction asks for its consumed capacity, and the function logs one line per batch:

    UNITS {"write_units": 4.0, "transactions": 2, "cancelled": 0}

which scripts/units.py reads (a cancelled transaction also bills its writes).
"""
import base64
import json
import os

import boto3

TABLE = os.environ.get("TABLE", "")
_ddb = None


def ddb():
    global _ddb
    _ddb = _ddb or boto3.client("dynamodb")
    return _ddb


def handler(event, context=None):
    units, transactions, cancelled = 0.0, 0, 0
    for record in event.get("Records", []):
        d = json.loads(base64.b64decode(record["kinesis"]["data"]))
        if d.get("event_type") != "trip_requested":
            continue
        transactions += 1
        try:
            r = ddb().transact_write_items(ReturnConsumedCapacity="TOTAL", TransactItems=[
                {"Put": {"TableName": TABLE, "Item": {"pk": {"S": f"event#{d['event_id']}"}},
                         "ConditionExpression": "attribute_not_exists(pk)"}},
                {"Update": {"TableName": TABLE, "Key": {"pk": {"S": f"zone#{d['pickup_zone_id']}"}},
                            "UpdateExpression": "ADD requested :one",
                            "ExpressionAttributeValues": {":one": {"N": "1"}}}}])
            units += sum(c.get("CapacityUnits", 0) for c in r.get("ConsumedCapacity", []))
        except ddb().exceptions.TransactionCanceledException:
            cancelled += 1
            units += 4.0          # a cancelled transaction bills its writes too (assumed: 2 items x 2 units)
    print("UNITS " + json.dumps({"write_units": units, "transactions": transactions, "cancelled": cancelled}))
    return {"transactions": transactions, "cancelled": cancelled}

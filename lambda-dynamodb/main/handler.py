"""The handler behind main/run.sh: the trip store's reads and writes in Python (boto3), as one Lambda handler.

This is the module a team copies (README, "Adapt it"). Each function is one access pattern from the dynamodb recipe,
and every write that two callers can race on carries a condition (README, "Decisions").

Events (from a Lambda function URL or API Gateway, or a test event):
  {"action": "get",    "trip_id": "t-0042", "strong": false}
  {"action": "rider",  "rider_id": "r-03", "since": "2026-10-03"}
  {"action": "claim",  "trip_id": "t-0042", "driver_id": "d-9"}
  {"action": "accept", "trip_id": "t-0101", "driver_id": "d-3"}      # transaction: trip + driver together

Environment: PREFIX (default "recipe"). The function's role needs dynamodb:GetItem, Query and UpdateItem on the three
tables (a transaction is authorised by the actions on its items). Local run:
  AWS_DEFAULT_REGION=ca-central-1 PREFIX=recipe python3 main/handler.py     (boto3 reads AWS_DEFAULT_REGION)
"""
import json
import os

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

PREFIX = os.environ.get("PREFIX", "recipe")
ddb = boto3.resource("dynamodb")          # the "resource" layer converts {"S": "x"} to plain Python values
trips = ddb.Table(f"{PREFIX}-trips")
rider_trips = ddb.Table(f"{PREFIX}-rider-trips")
client = boto3.client("dynamodb")       # transactions: the plain client, typed values ({"S": ...}); ddb.meta.client
                                          # would type them a second time and fail with ValidationError


def get_trip(trip_id, strong=False):
    r = trips.get_item(Key={"trip_id": trip_id}, ConsistentRead=strong, ReturnConsumedCapacity="TOTAL")
    return {"item": r.get("Item"), "read_units": r["ConsumedCapacity"]["CapacityUnits"]}


def rider_history(rider_id, since):
    cond = Key("rider_id").eq(rider_id)
    if since:                                 # optional: only trips requested at or after this time
        cond = cond & Key("requested_at").gte(since)
    r = rider_trips.query(KeyConditionExpression=cond, ReturnConsumedCapacity="TOTAL")
    return {"items": r["Items"], "read_units": r["ConsumedCapacity"]["CapacityUnits"]}


def claim(trip_id, driver_id):
    # Decision (conditional write): checked and written in one step, so two drivers cannot both claim the trip.
    # A refusal is an answer, not an error: the caller learns who holds the trip.
    try:
        trips.update_item(Key={"trip_id": trip_id},
                          UpdateExpression="SET driver_id = :d, #s = :s",
                          ConditionExpression="attribute_not_exists(driver_id)",
                          ExpressionAttributeNames={"#s": "status"},
                          ExpressionAttributeValues={":d": driver_id, ":s": "accepted"},
                          ReturnValuesOnConditionCheckFailure="ALL_OLD")
        return {"claimed": True}
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        holder = e.response.get("Item", {}).get("driver_id", {}).get("S")
        return {"claimed": False, "held_by": holder}


def accept(trip_id, driver_id):
    # Decision (transaction): the trip and the driver must agree, so both updates go in one transaction, all or none.
    try:
        client.transact_write_items(TransactItems=[
            {"Update": {"TableName": f"{PREFIX}-trips", "Key": {"trip_id": {"S": trip_id}},
                        "UpdateExpression": "SET driver_id = :d, #s = :a",
                        "ConditionExpression": "attribute_not_exists(driver_id)",
                        "ExpressionAttributeNames": {"#s": "status"},
                        "ExpressionAttributeValues": {":d": {"S": driver_id}, ":a": {"S": "accepted"}}}},
            {"Update": {"TableName": f"{PREFIX}-drivers", "Key": {"driver_id": {"S": driver_id}},
                        "UpdateExpression": "SET #s = :busy, trip_id = :t",
                        "ConditionExpression": "#s = :available",
                        "ExpressionAttributeNames": {"#s": "status"},
                        "ExpressionAttributeValues": {":busy": {"S": "busy"}, ":available": {"S": "available"},
                                                      ":t": {"S": trip_id}}}}])
        return {"accepted": True}
    except ClientError as e:
        if e.response["Error"]["Code"] != "TransactionCanceledException":
            raise
        reasons = [r["Code"] for r in e.response.get("CancellationReasons", [])]
        return {"accepted": False, "reasons": dict(zip(["trip", "driver"], reasons))}


def number(v):                                # boto3 returns DynamoDB numbers as Decimal
    return int(v) if v == int(v) else float(v)


# Decision (API shape): the fields each action needs. An id is a string of 1 to MAX_ID characters; anything else is
# answered with 400 before DynamoDB is called, so a caller's mistake is the caller's error and not a crash here.
FIELDS = {"get": ("trip_id",), "rider": ("rider_id",), "claim": ("trip_id", "driver_id"), "accept": ("trip_id", "driver_id")}
MAX_ID = 64


def bad_request(message):
    return {"statusCode": 400, "body": json.dumps({"error": message})}


def outcome(action, status, out):
    """What happened, in one word, for the log line: no id of a rider, a driver or a trip is in it."""
    if status != 200:
        return "bad_request"
    if action == "claim":
        return "claimed" if out["claimed"] else "refused"
    if action == "accept":
        return "accepted" if out["accepted"] else "cancelled"
    if action == "get":
        return "found" if out["item"] else "not_found"
    return "listed"


def lambda_handler(event, context=None):
    """One request. Writes one JSON log line when it ends (README, Decisions: log line), then returns the reply."""
    # Decision (log line): JSON, so CloudWatch Logs can filter on its fields; the Lambda request id links it to the
    # REPORT line of the same call. No ids of people or trips, and nothing the caller typed except a known action.
    line = {"request_id": getattr(context, "aws_request_id", None), "action": None, "status": None, "outcome": "error"}
    try:
        reply, line["action"], out = handle(event)
        line["status"], line["outcome"] = reply["statusCode"], outcome(line["action"], reply["statusCode"], out)
        return reply
    except Exception as err:
        # A call that DynamoDB refuses (a throttle, a missing permission) ends the function with an error. Without
        # this line the log would hold Lambda's stack trace and nothing that says which action failed.
        request, _ = check(event)
        line["action"] = request["action"] if request else None
        line["error"] = getattr(err, "response", {}).get("Error", {}).get("Code") or type(err).__name__
        raise
    finally:
        print(json.dumps(line))


def handle(event):
    """(reply, action, result). action is None unless it is one of the four the API has."""
    event, refusal = check(event)
    if refusal:
        return refusal, None, None
    a = event["action"]
    if a == "get":
        out = get_trip(event["trip_id"], bool(event.get("strong")))
    elif a == "rider":
        out = rider_history(event["rider_id"], event.get("since", ""))
    elif a == "claim":
        out = claim(event["trip_id"], event["driver_id"])
    else:
        out = accept(event["trip_id"], event["driver_id"])
    return {"statusCode": 200, "body": json.dumps(out, default=number)}, a, out


def check(event):
    """(request, None) for a request with a known action and its fields, or (None, the 400 reply that says what is
    wrong). Two values, so that nothing a caller sends can be taken for a reply."""
    if "body" in event:                   # function URL / API Gateway: the request is in the body
        try:
            event = json.loads(event["body"] or "{}")
        except ValueError:
            return None, bad_request("the body is not JSON")
    if not isinstance(event, dict):
        return None, bad_request("the body is not a JSON object")
    a = event.get("action")
    # Decision (API shape): a fixed list of actions; anything else is refused with 400, so the URL cannot run other code.
    if not isinstance(a, str) or a not in FIELDS:
        return None, bad_request(f"unknown action {a!r}")
    for name in FIELDS[a]:
        value = event.get(name)
        if not isinstance(value, str) or not 1 <= len(value) <= MAX_ID:
            return None, bad_request(f"{name} must be a string of 1 to {MAX_ID} characters")
    since = event.get("since", "")
    if not isinstance(since, str) or len(since) > MAX_ID:
        return None, bad_request(f"since must be a string of at most {MAX_ID} characters")
    return event, None


if __name__ == "__main__":                # local test against your tables, after the dynamodb recipe's main and
                                          # ext-riders-and-drivers with KEEP=1
    for ev in [{"action": "get", "trip_id": "t-0042"},
               {"action": "get", "trip_id": "t-0042", "strong": True},
               {"action": "rider", "rider_id": "r-03", "since": "2026-10-08"},
               {"action": "claim", "trip_id": "t-0042", "driver_id": "d-9"},
               {"action": "accept", "trip_id": "t-0101", "driver_id": "d-3"}]:
        print(json.dumps(ev), "->", handle(ev)[0]["body"])     # handle, not lambda_handler: no log lines here

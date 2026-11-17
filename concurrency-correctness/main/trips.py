"""The trips table and the two ways a driver's app can accept a trip.

A trip item: trip_id, status (requested | accepted), driver_id, version. Accepting is a read-modify-write: the app
reads the trip, checks that it is still requested, and writes it back as accepted by this driver.

- accept_unsafe writes without a condition. When two apps read before either writes, both writes succeed, the second
  replaces the first, and both drivers are told they got the trip: a lost update.
- accept_safe writes on the condition that the version is still the one the app read (optimistic concurrency, a
  compare-and-set). DynamoDB checks the condition and writes in one step, so only one of the two writes succeeds;
  the other gets ConditionalCheckFailedException and the item that blocked it.

README "Decisions" explains each choice.
"""

TABLE_KEY = "trip_id"
# A ceiling on each table's on-demand throughput, in request units per second (README, Decisions: Table). A loop
# that races without end is throttled at this rate instead of billing without limit.
MAX_UNITS = 100
CEILING = {"MaxReadRequestUnits": MAX_UNITS, "MaxWriteRequestUnits": MAX_UNITS}


def table_name(prefix: str) -> str:
    return f"{prefix}-race-trips"


def create_table(ddb, name: str, poll_s: float = 2.0) -> None:
    for _ in range(2):
        try:
            ddb.create_table(TableName=name, BillingMode="PAY_PER_REQUEST", OnDemandThroughput=CEILING,
                             KeySchema=[{"AttributeName": TABLE_KEY, "KeyType": "HASH"}],
                             AttributeDefinitions=[{"AttributeName": TABLE_KEY, "AttributeType": "S"}],
                             Tags=[{"Key": "purpose", "Value": "recipe-concurrency-correctness"}])
            break
        except ddb.exceptions.ResourceInUseException:
            # ResourceInUseException is the answer for a table that is there (API reference, CreateTable). We also
            # take it for one that is still being deleted (a clean-up that was stopped before it finished), which
            # that page does not say: that one is waited out and created again.
            try:
                status = ddb.describe_table(TableName=name)["Table"]["TableStatus"]
            except ddb.exceptions.ResourceNotFoundException:
                continue
            if status != "DELETING":
                break
            ddb.get_waiter("table_not_exists").wait(TableName=name, WaiterConfig={"Delay": poll_s, "MaxAttempts": int(300 / max(poll_s, 1))})
    ddb.get_waiter("table_exists").wait(TableName=name, WaiterConfig={"Delay": poll_s, "MaxAttempts": int(300 / max(poll_s, 1))})


def reset_trip(ddb, table: str, trip_id: str) -> None:
    """A requested trip with no driver, version 1."""
    ddb.put_item(TableName=table, Item={"trip_id": {"S": trip_id}, "status": {"S": "requested"},
                                        "version": {"N": "1"}, "tip_total": {"N": "0"}})


def read_trip(ddb, table: str, trip_id: str) -> dict:
    """Strongly consistent read: the app must see the latest accepted write before it decides (L12)."""
    item = ddb.get_item(TableName=table, Key={"trip_id": {"S": trip_id}}, ConsistentRead=True)["Item"]
    return {"status": item["status"]["S"], "version": int(item["version"]["N"]),
            "driver_id": item.get("driver_id", {}).get("S"), "tip_total": int(item.get("tip_total", {"N": "0"})["N"])}


def _accept_args(table, trip_id, driver_id, seen_version):
    return dict(TableName=table, Key={"trip_id": {"S": trip_id}},
                UpdateExpression="SET driver_id = :d, #s = :accepted, version = :next",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":d": {"S": driver_id}, ":accepted": {"S": "accepted"},
                                           ":next": {"N": str(seen_version + 1)}})


def accept_unsafe(ddb, table: str, trip_id: str, driver_id: str, seen_version: int) -> dict:
    """No condition: last write wins, and every caller is told it succeeded."""
    ddb.update_item(**_accept_args(table, trip_id, driver_id, seen_version))
    return {"driver": driver_id, "accepted": True}


def accept_safe(ddb, table: str, trip_id: str, driver_id: str, seen_version: int) -> dict:
    """Write only if the version is still the one this app read; otherwise report who has the trip."""
    args = _accept_args(table, trip_id, driver_id, seen_version)
    args["ConditionExpression"] = "version = :seen"
    args["ExpressionAttributeValues"][":seen"] = {"N": str(seen_version)}
    args["ReturnValuesOnConditionCheckFailure"] = "ALL_OLD"       # the refusal carries the item that blocked it
    try:
        ddb.update_item(**args)
        return {"driver": driver_id, "accepted": True}
    except ddb.exceptions.ConditionalCheckFailedException as err:
        holder = err.response.get("Item", {}).get("driver_id", {}).get("S")
        if holder == driver_id:
            # This driver's own earlier accept was applied and its reply was lost: the app sent the accept again,
            # and the version it read is now old. The trip is this driver's, so the answer is yes, not a refusal.
            return {"driver": driver_id, "accepted": True, "repeat": True}
        return {"driver": driver_id, "accepted": False, "held_by": holder}

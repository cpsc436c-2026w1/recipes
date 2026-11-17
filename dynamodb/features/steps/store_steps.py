"""Steps that read what a run against the local stand-in (moto) printed and left in its tables."""

from __future__ import annotations

import json
import re

import support
from behave import given, then


def _lst(text: str) -> list[str]:
    return [t.strip() for t in text.split(",") if t.strip()]


@then('step "{n}" created the table "{table}" with partition key "{key}" and billing mode "{mode}"')
def step_created(context, n, table, key, mode):
    cmd = next(c for c in context.run.step_cmds(n) if c.startswith("aws dynamodb create-table"))
    assert f"--table-name {table} " in cmd and f"--billing-mode {mode} " in cmd, cmd
    described = support.ddb(context).describe_table(TableName=table)["Table"]
    assert described["KeySchema"] == [{"AttributeName": key, "KeyType": "HASH"}], described["KeySchema"]
    assert described["BillingModeSummary"]["BillingMode"] == mode


@then('the table "{table}" has the index "{index}" with partition key "{pk}" and sort key "{sk}"')
def step_index(context, table, index, pk, sk):
    (gsi,) = support.ddb(context).describe_table(TableName=table)["Table"]["GlobalSecondaryIndexes"]
    assert gsi["IndexName"] == index, gsi
    assert gsi["KeySchema"] == [{"AttributeName": pk, "KeyType": "HASH"}, {"AttributeName": sk, "KeyType": "RANGE"}]


@then('the command of step "{n}" is a Query on the index "{index}"')
def step_query_index(context, n, index):
    (cmd,) = context.run.step_cmds(n)
    assert cmd.startswith("aws dynamodb query ") and f"--index-name {index} " in cmd, cmd


@then('step "{n}" says the Query returned as many items as wait at "{zone}" in that table')
def step_query_returned(context, n, zone):
    waiting = [i for i in support.items(context, f"{context.prefix}-trips")
               if i["pickup_zone"] == zone and i["status"] == "requested"]
    assert context.run.step_json(n)["returned"] == len(waiting) > 0, context.run.step_json(n)


@then('the table "{table}" has the tags "{tags}"')
def step_tags(context, table, tags):
    client = support.ddb(context)
    arn = client.describe_table(TableName=table)["Table"]["TableArn"]
    found = {t["Key"]: t["Value"] for t in client.list_tags_of_resource(ResourceArn=arn)["Tags"]}
    assert found == dict(t.split("=") for t in _lst(tags)), found


@then('the local stand-in has exactly the tables "{tables}"')
def step_tables(context, tables):
    found = sorted(support.ddb(context).list_tables()["TableNames"])
    assert found == sorted(_lst(tables)), found


@given('the tables "{tables}" of other owners exist')
def step_other_tables(context, tables):
    for name in _lst(tables):
        support.ddb(context).create_table(
            TableName=name, BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}])


@then('step "{n}" made {calls:d} batch-write-item calls, each with {size:d} items')
def step_batches(context, n, calls, size):
    cmds = [c for c in context.run.step_cmds(n) if c.startswith("aws dynamodb batch-write-item")]
    assert len(cmds) == calls, len(cmds)
    for cmd in cmds:
        (requests,) = support.request_items(cmd).values()
        assert len(requests) == size, len(requests)


@then('the table "{table}" holds {n:d} trips')
def step_holds(context, table, n):
    assert len(support.items(context, table)) == n


@then('trip "{trip}" has driver "{driver}" and status "{status}"')
def step_trip_driver(context, trip, driver, status):
    item = support.item(context, f"{context.prefix}-trips", trip_id=trip)
    assert item and item.get("driver_id") == driver and item["status"] == status, item


@then('trip "{trip}" has no driver and status "{status}"')
def step_trip_free(context, trip, status):
    item = support.item(context, f"{context.prefix}-trips", trip_id=trip)
    assert item and "driver_id" not in item and item["status"] == status, item


@then('driver "{driver}" has trip "{trip}" and status "{status}"')
def step_driver(context, driver, trip, status):
    item = support.item(context, f"{context.prefix}-drivers", driver_id=driver)
    assert item and item.get("trip_id") == trip and item["status"] == status, item


@then('trip "{trip}" has fare "{fare}" and version "{version}"')
def step_fare(context, trip, fare, version):
    item = support.item(context, f"{context.prefix}-trips", trip_id=trip)
    assert item and item["fare_quote"] == fare and item["version"] == version, item


@then('trip "{trip}" is in the table "{table}"')
def step_trip_in(context, trip, table):
    assert support.item(context, table, trip_id=trip), f"{trip} is not in {table}"


@then('the error output shows the blocking item with driver "{driver}"')
def step_blocking(context, driver):
    err = context.run.err
    m = re.search(r"\{.*\}", err, flags=re.S)
    assert m, err
    item = json.loads(m.group(0))["Item"]
    assert item["driver_id"]["S"] == driver and item["trip_id"]["S"] == "t-0042", item


@then('the output of step "{n}" holds "{text}"')
def step_step_holds(context, n, text):
    assert text in context.run.step(n), context.run.step(n)


@then('the get-item command of step "{n}" has "{flag}"')
def step_get_flag(context, n, flag):
    (cmd,) = [c for c in context.run.step_cmds(n) if c.startswith("aws dynamodb get-item")]
    assert flag in cmd.split(), cmd


@then('step "{n}" printed driver "{driver}" and status "{status}"')
def step_printed_driver(context, n, driver, status):
    assert context.run.step_json(n) == {"driver": driver, "status": status}, context.run.step_json(n)


@then('step "{n}" says the Scan read as many items as the table "{table}" holds')
def step_scan_read(context, n, table):
    assert context.run.step_json(n)["read"] == len(support.items(context, table)) > 0


@then('step "{n}" says the Scan returned as many items as wait at "{zone}" in that table')
def step_scan_returned(context, n, zone):
    waiting = [i for i in support.items(context, f"{context.prefix}-trips")
               if i["pickup_zone"] == zone and i["status"] == "requested"]
    answer = context.run.step_json(n)
    assert answer["returned"] == len(waiting) > 0 and answer["returned"] < answer["read"], answer


@then('every command of steps "{steps}" asks for the consumed units')
def step_units_asked(context, steps):
    for n in _lst(steps):
        cmds = [c for c in context.run.step_cmds(n) if c.startswith("aws dynamodb")]
        assert cmds and all("--return-consumed-capacity TOTAL" in c for c in cmds), (n, cmds)


@then('the answers of steps "{steps}" name "{field}"')
def step_units_named(context, steps, field):
    for n in _lst(steps):
        assert isinstance(context.run.step_json(n)[field], (int, float)), (n, context.run.step_json(n))


# ---- riders and drivers

@then('step "{n}" returned the trips of rider "{rider}" from "{a}" to "{b}" that the table "{table}" holds')
def step_history(context, n, rider, a, b, table):
    expected = sorted([i["requested_at"], i["trip_id"]] for i in support.items(context, table)
                      if i["rider_id"] == rider and a <= i["requested_at"] <= b)
    context.history = context.run.step_json(n)
    assert expected and sorted(context.history["trips"]) == expected, (context.history, expected)


@then('the trips of step "{n}" are in time order')
def step_time_order(context, n):
    times = [t[0] for t in context.history["trips"]]
    assert times == sorted(times) and len(set(times)) == len(times), times


@then('step "{n}" says the Query read as many items as it returned')
def step_query_read(context, n):
    answer = context.run.step_json(n)
    returned = answer["returned"] if "returned" in answer else len(answer["trips"])
    assert answer["read"] == returned > 0, answer


@then('step "{n}" returned the newest of the loaded trips of rider "{rider}"')
def step_latest(context, n, rider):
    loaded = sorted([i["requested_at"], i["trip_id"]] for i in support.items(context, f"{context.prefix}-rider-trips")
                    if i["rider_id"] == rider and i["trip_id"].startswith("h-"))
    assert context.run.step_json(n) == loaded[-1], (context.run.step_json(n), loaded[-1])
    (cmd,) = context.run.step_cmds(n)
    assert "--no-scan-index-forward" in cmd and "--limit 1" in cmd, cmd


@then('step "{n}" made 1 transaction with a Put in "{first}" and a Put in "{second}"')
def step_transaction(context, n, first, second):
    (cmd,) = context.run.step_cmds(n)
    assert cmd.startswith("aws dynamodb transact-write-items"), cmd
    tx = sorted(context.shared_tmp.glob("*-tx.json"))
    assert tx, "the script wrote no transaction file"
    # the file on disk is the last transaction of the run; the one of this step is read from the stand-in's tables
    for table in (first, second):
        assert support.item(context, table, trip_id="t-0100"), table


@given("the items of the three tables are noted")
def step_note(context):
    context.noted = _snapshot(context)


@then("the three tables hold the items that were noted")
def step_same(context):
    assert _snapshot(context) == context.noted


def _snapshot(context) -> dict:
    return {t: sorted(support.items(context, f"{context.prefix}-{t}"), key=lambda i: json.dumps(i, sort_keys=True))
            for t in ("trips", "rider-trips", "drivers")}

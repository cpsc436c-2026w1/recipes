"""Steps that call main/handler.py inside moto, the way a function URL calls it."""

from __future__ import annotations

import json

import support
from behave import given, then, when


def _lst(text: str) -> list[str]:
    return [t.strip() for t in text.split(",") if t.strip()]


@given('the handler with the tables of PREFIX "{prefix}"')
def step_handler(context, prefix):
    support.load_handler(context, prefix)


@given('the trips "{trips}" are waiting, each with a fare')
def step_trips(context, trips):
    for i, trip in enumerate(_lst(trips)):
        context.ddb.put_item(TableName=f"{context.prefix}-trips",
                             Item={"trip_id": {"S": trip}, "status": {"S": "requested"}, "fare_quote": {"N": str(20 + i)}})


@given('the drivers "{drivers}" are available')
def step_drivers(context, drivers):
    for driver in _lst(drivers):
        context.ddb.put_item(TableName=f"{context.prefix}-drivers",
                             Item={"driver_id": {"S": driver}, "status": {"S": "available"}})


@given('rider "{rider}" has trips requested at "{times}"')
def step_rider(context, rider, times):
    for i, t in enumerate(_lst(times)):
        context.ddb.put_item(TableName=f"{context.prefix}-rider-trips",
                             Item={"rider_id": {"S": rider}, "requested_at": {"S": t}, "trip_id": {"S": f"h-{i}"}})


@when('a caller sends action "{action}" with trip_id "{trip}"')
def step_get(context, action, trip):
    support.call(context, action=action, trip_id=trip)


@when('a caller sends action "{action}" with trip_id "{trip}" and strong true')
def step_get_strong(context, action, trip):
    support.call(context, action=action, trip_id=trip, strong=True)


@when('a caller sends action "{action}" with rider_id "{rider}"')
def step_rider_all(context, action, rider):
    support.call(context, action=action, rider_id=rider)


@when('a caller sends action "{action}" with rider_id "{rider}" from "{since}" on')
def step_rider_since(context, action, rider, since):
    support.call(context, action=action, rider_id=rider, since=since)


@given('driver "{driver}" sent action "{action}" for trip "{trip}"')
@when('driver "{driver}" sends action "{action}" for trip "{trip}"')
def step_write(context, driver, action, trip):
    support.call(context, action=action, trip_id=trip, driver_id=driver)


@when('a caller sends action "{action}" with a trip_id of {n:d} characters')
def step_long_id(context, action, n):
    support.call(context, action=action, trip_id="t" * n)


@when("a function URL passes on the raw body {raw}")
def step_raw(context, raw):
    context.ddb_calls.clear()
    reply = context.handler.lambda_handler({"body": raw})
    context.status, context.reply = reply["statusCode"], json.loads(reply["body"])


class _Context:
    """What Lambda passes as the second argument, as far as the handler uses it."""

    def __init__(self, request_id):
        self.aws_request_id = request_id


@when('Lambda calls the handler with request id "{request_id}" and the body {body}')
def step_lambda_calls(context, request_id, body):
    import contextlib
    import io

    out = io.StringIO()
    context.ddb_calls.clear()
    with contextlib.redirect_stdout(out):
        reply = context.handler.lambda_handler({"body": body}, _Context(request_id))
    context.status, context.reply, context.printed = reply["statusCode"], json.loads(reply["body"]), out.getvalue()


@given('DynamoDB refuses every "{operation}" call with "{code}"')
def step_ddb_refuses(context, operation, code):
    from botocore.exceptions import ClientError

    def refuse(params, model, **_):
        if model.name == operation:
            raise ClientError({"Error": {"Code": code, "Message": "put in by the scenario"}}, operation)

    for client in (context.handler.ddb.meta.client, context.handler.client):
        client.meta.events.register("provide-client-params.dynamodb.*", refuse)


@when('the function fails when Lambda calls the handler with request id "{request_id}" and the body {body}')
def step_lambda_calls_fails(context, request_id, body):
    import contextlib
    import io

    out = io.StringIO()
    context.error = None
    with contextlib.redirect_stdout(out):
        try:
            context.handler.lambda_handler({"body": body}, _Context(request_id))
        except Exception as err:     # noqa: BLE001  the function fails, as it does in Lambda
            context.error = err
    assert context.error is not None, "the handler returned a reply"
    context.printed = out.getvalue()


@then('the log line\'s error is "{code}"')
def step_log_error(context, code):
    assert json.loads(context.printed.strip())["error"] == code, context.printed


@then('the handler printed 1 line, a JSON object with exactly the fields "{fields}"')
def step_log_fields(context, fields):
    lines = context.printed.splitlines()
    assert len(lines) == 1, lines
    assert list(json.loads(lines[0])) == _lst(fields), lines[0]


@then('the log line has request_id "{request_id}", action {action}, status {status:d} and outcome "{outcome}"')
def step_log_line(context, request_id, action, status, outcome):
    line = json.loads(context.printed.splitlines()[-1])
    assert line == {"request_id": request_id, "action": json.loads(action), "status": status, "outcome": outcome}, line
    assert context.status == status


@then('the log line has request_id "{request_id}", action {action}, status null and outcome "{outcome}"')
def step_log_line_error(context, request_id, action, outcome):
    line = json.loads(context.printed.splitlines()[-1])
    line.pop("error")
    assert line == {"request_id": request_id, "action": json.loads(action), "status": None, "outcome": outcome}, line


@then('the handler\'s output holds none of "{values}"')
def step_no_values(context, values):
    assert context.printed.strip(), "the handler printed nothing"
    for value in _lst(values):
        assert value not in context.printed, (value, context.printed)


@when("a caller sends the body {body}")
def step_body(context, body):
    support.call(context, **json.loads(body))


@then("the status is {status:d}")
def step_status(context, status):
    assert context.status == status, (context.status, context.reply)


@then("the reply is {reply}")
def step_reply(context, reply):
    assert context.reply == json.loads(reply), context.reply


@then('the reply\'s item has trip_id "{trip}"')
def step_item(context, trip):
    assert context.reply["item"]["trip_id"] == trip, context.reply


@then("the reply's item has the fare as a JSON number")
def step_number(context):
    fare = context.reply["item"]["fare_quote"]
    assert isinstance(fare, int) and not isinstance(fare, bool), repr(fare)


@then("the reply names the read units of the call")
def step_units(context):
    assert isinstance(context.reply["read_units"], (int, float)), context.reply


@then('the handler read the table "{table}" with ConsistentRead {value}')
def step_consistent(context, table, value):
    (op, params), = context.ddb_calls
    assert op == "GetItem" and params["TableName"] == table, (op, params)
    assert params["ConsistentRead"] is (value == "true"), params


@then("the reply has {n:d} items in time order")
def step_items(context, n):
    times = [i["requested_at"] for i in context.reply["items"]]
    assert len(times) == n and times == sorted(times), times


@then('every item of the reply was requested at or after "{since}"')
def step_since(context, since):
    assert all(i["requested_at"] >= since for i in context.reply["items"]), context.reply


@then("the handler made {n:d} call to DynamoDB, a {op}")
def step_one_call(context, n, op):
    assert [name for name, _ in context.ddb_calls] == [op] * n, context.ddb_calls


@then("the handler made 0 calls to DynamoDB")
def step_no_calls(context):
    assert context.ddb_calls == [], context.ddb_calls


@then('the reply\'s error holds "{text}"')
def step_error(context, text):
    assert text in context.reply["error"], context.reply


@then('trip "{trip}" has driver "{driver}" and status "{status}"')
def step_trip(context, trip, driver, status):
    found = support.item(context, "trips", trip_id=trip)
    assert found.get("driver_id") == driver and found["status"] == status, found


@then('trip "{trip}" has no driver and status "{status}"')
def step_trip_free(context, trip, status):
    found = support.item(context, "trips", trip_id=trip)
    assert "driver_id" not in found and found["status"] == status, found


@then('driver "{driver}" has trip "{trip}" and status "{status}"')
def step_driver(context, driver, trip, status):
    found = support.item(context, "drivers", driver_id=driver)
    assert found.get("trip_id") == trip and found["status"] == status, found


@then('driver "{driver}" has no trip and status "{status}"')
def step_driver_free(context, driver, status):
    found = support.item(context, "drivers", driver_id=driver)
    assert "trip_id" not in found and found["status"] == status, found

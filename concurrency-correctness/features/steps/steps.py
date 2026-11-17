"""Steps for the three features. Each When runs an entry point of the recipe: main/run.py, clean_up.py,
ext-idempotent-retry/run.py, or a function of tips.py that a team copies."""

from __future__ import annotations

import os
import re
import time

import support
from behave import given, then, when


def _lst(text: str) -> list[str]:
    return [t.strip() for t in text.split(",") if t.strip()]


# ---- set-up

@given('AWS refuses every "{operation}" call with "{code}"')
def step_refuse_all(context, operation, code):
    context.calls.refuse(operation, code)


@given('AWS refuses the "{operation}" call for "{table}" with "{code}"')
def step_refuse_table(context, operation, table, code):
    context.calls.refuse(operation, code, when=lambda p: p.get("TableName") == table)


@given('the table "{table}" is in the DELETING state until the recipe has waited for it')
def step_deleting(context, table):
    # CreateTable on a name that is still on its way out is refused once, as for a table that exists
    context.calls.refuse("CreateTable", "ResourceInUseException", times=1, when=lambda p: p.get("TableName") == table)
    context.calls.deleting(table)


@given('no "{operation}" call gets a reply')
def step_no_reply(context, operation):
    context.calls.no_reply(operation)


@given("the recipe's waits are written down, not waited")
def step_no_waits(context):
    real = time.sleep
    time.sleep = lambda s: context.sleeps.append(s)
    context.add_cleanup(setattr, time, "sleep", real)


@given("no AWS credentials and no Region in the environment")
def step_no_credentials(context):
    for name in [n for n in os.environ if n.startswith("AWS_") and n != "AWS_EC2_METADATA_DISABLED"]:
        os.environ.pop(name)


@given('AWS_REGION is "{value}"')
def step_region(context, value):
    if value != "not set":
        os.environ["AWS_REGION"] = value


@given('the two tables of PREFIX "{prefix}" exist')
def step_two_tables(context, prefix):
    import tips
    import trips

    trips.create_table(support.ddb(), trips.table_name(prefix), poll_s=0)
    tips.create_requests_table(support.ddb(), tips.requests_table_name(prefix), poll_s=0)
    context.calls.made.clear()
    context.calls.regions.clear()


@given('the tables "{names}" of other owners exist')
def step_other_tables(context, names):
    for name in _lst(names):
        support.ddb().create_table(TableName=name, BillingMode="PAY_PER_REQUEST",
                                   KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
                                   AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}])
    context.calls.made.clear()


# ---- running

@given('main/run.py ran with "{args}"')
def step_main_ran(context, args):
    support.run_main(context, "main/run.py", args.split())
    assert context.exit_code == 0 and context.error is None, context.printed
    context.calls.made.clear()
    context.printed = ""


@when('main/run.py runs with "{args}"')
def step_main(context, args):
    support.run_main(context, "main/run.py", args.split())


@when('with PREFIX "{prefix}", main/run.py runs with the arguments "{args}"')
def step_main_prefix(context, prefix, args):
    support.run_main(context, "main/run.py", args.split(), None if prefix == "not set" else prefix)


@given('ext-idempotent-retry/run.py ran with "{args}"')
def step_ext_ran(context, args):
    support.run_main(context, "ext-idempotent-retry/run.py", args.split())
    assert context.exit_code == 0 and context.error is None, context.printed
    context.calls.made.clear()
    context.printed = ""


@when('ext-idempotent-retry/run.py runs with "{args}"')
def step_ext(context, args):
    support.run_main(context, "ext-idempotent-retry/run.py", args.split())


@when('ext-idempotent-retry/run.py runs with "{args}", {n:d} times')
def step_ext_n(context, args, n):
    context.runs_waits = []
    for _ in range(n):
        context.sleeps.clear()
        support.run_main(context, "ext-idempotent-retry/run.py", args.split())
        context.runs_waits.append(list(context.sleeps))


@then("each run waited {n:d} times: at most {first:f} seconds, then at most {second:f} seconds")
def step_bounded_waits(context, n, first, second):
    for waits in context.runs_waits:
        assert len(waits) == n and 0 <= waits[0] <= first and 0 <= waits[1] <= second, waits


@then("the {n:d} first waits are not all the same")
def step_random_waits(context, n):
    firsts = {waits[0] for waits in context.runs_waits}
    assert len(context.runs_waits) == n and len(firsts) > 1, firsts


@when("clean_up.py runs")
def step_clean_up(context):
    support.run_clean_up(context)


@when('add_tip_once is called for trip "{trip}" with a new request id')
def step_add_tip_once(context, trip):
    import tips
    import trips

    support.run(context, tips.add_tip_once, support.ddb(), trips.table_name(support.PREFIX), trip, 5,
                tips.new_request_id(), tips.requests_table_name(support.PREFIX))


# ---- what a run printed and left

@then("the exit code is {code:d}")
def step_code(context, code):
    assert context.exit_code == code and (context.error is None or code != 0), (context.exit_code, context.error,
                                                                               context.printed[-1500:])


@then('the output holds "{text}"')
def step_out(context, text):
    assert text in context.printed, context.printed[-2000:]


@then('the output does not hold "{text}"')
def step_out_not(context, text):
    assert text not in context.printed, context.printed[-2000:]


@then('{n:d} printed line starts with "{start}"')
@then('{n:d} printed lines start with "{start}"')
def step_lines(context, n, start):
    found = [line for line in context.printed.splitlines() if line.startswith(start)]
    assert len(found) == n, context.printed


@then("the account has no tables")
def step_no_tables(context):
    assert support.tables() == [], support.tables()


@then('the account has exactly the tables "{names}"')
def step_tables(context, names):
    assert support.tables() == sorted(_lst(names)), support.tables()


@then("no DeleteTable call was made")
def step_no_delete(context):
    assert "DeleteTable" not in context.calls.names()


@then('{n:d} DeleteTable call was made, for "{table}"')
def step_one_delete(context, n, table):
    assert [p["TableName"] for p in context.calls.of("DeleteTable")] == [table] * n


@then("the recipe waited for each table to be gone after its delete")
def step_waited_gone(context):
    names = context.calls.made
    deletes = [i for i, (op, _) in enumerate(names) if op == "DeleteTable"]
    assert len(deletes) == 2
    for i in deletes:
        op, params = names[i + 1]
        assert op == "DescribeTable" and params["TableName"] == names[i][1]["TableName"], names[i + 1]


@then('every call that names a table names "{table}"')
def step_table_names(context, table):
    found = {p["TableName"] for _, p in context.calls.made if "TableName" in p}
    assert found == {table}, found


@then('every call went to the Region "{region}"')
def step_regions(context, region):
    assert context.exit_code == 0, context.printed
    assert context.calls.regions and set(context.calls.regions) == {region}, set(context.calls.regions)


@then("{n:d} CreateTable calls were made, each with a maximum of {r:d} read and {w:d} write request units per second")
def step_ceiling(context, n, r, w):
    created = context.calls.of("CreateTable")
    assert len(created) == n, len(created)
    for params in created:
        assert params["OnDemandThroughput"] == {"MaxReadRequestUnits": r, "MaxWriteRequestUnits": w}, params


# ---- the race

@then('in "{mode}" mode {n:d} of {rounds:d} rounds told both drivers yes and {one:d} rounds had one winner')
def step_unsafe(context, mode, n, rounds, one):
    row = support.report_rows(context.printed)[mode]
    assert row == {"rounds": rounds, "one_winner": one, "both_told_yes": n, "winner_is_stored": 0}, row


@then('in "{mode}" mode {n:d} of {rounds:d} rounds had one winner, {both:d} rounds told both drivers yes, and the winner is stored in {stored:d}')
def step_safe(context, mode, n, rounds, both, stored):
    row = support.report_rows(context.printed)[mode]
    assert row == {"rounds": rounds, "one_winner": n, "both_told_yes": both, "winner_is_stored": stored}, row


@then('every trip of the "safe" rounds holds one driver and version {version:d}')
def step_trips_after(context, version):
    items = support.ddb().scan(TableName=f"{support.PREFIX}-race-trips", ConsistentRead=True)["Items"]
    rounds = [i for i in items if i["trip_id"]["S"].startswith("t-race-")]
    assert rounds and all(i["version"]["N"] == str(version) and i["driver_id"]["S"] in ("d-7", "d-9")
                          and i["status"]["S"] == "accepted" for i in rounds), rounds


@then('the printed race on trip "{trip}" has one driver "accepted" and one "refused, the trip is held by" the other')
def step_one_race(context, trip):
    block = context.printed.split(f"one race on trip {trip}, safe mode:")[1]
    told = dict(re.findall(r"^  (d-\d): (.*)$", block, flags=re.M))
    winners = [d for d, text in told.items() if text == "accepted"]
    assert len(told) == 2 and len(winners) == 1, told
    (loser,) = [d for d in told if d not in winners]
    assert told[loser] == f"refused, the trip is held by {winners[0]}", told
    context.winner = winners[0]


@then('under it one line says a driver was "accepted", one that the other was "refused, the trip is held by" the '
      "first, and one what the table holds")
def step_race_lines(context):
    block = context.printed.split("one race on trip t-0042, safe mode:")[1].strip().splitlines()
    told = [line.strip() for line in block[:3]]
    accepted = [t for t in told if re.fullmatch(r"d-\d: accepted", t)]
    refused = [t for t in told if re.fullmatch(r"d-\d: refused, the trip is held by d-\d", t)]
    assert len(accepted) == 1 and len(refused) == 1, told
    winner = accepted[0].split(":")[0]
    assert refused[0].endswith(winner) and not refused[0].startswith(winner), told
    assert told[2] == f"table: driver {winner}, version 2", told


@then("the table line of that race names the accepted driver and version {version:d}")
def step_table_line(context, version):
    assert f"  table: driver {context.winner}, version {version}" in context.printed, context.printed[-400:]


@then("every conditional write asked for the item that blocks it")
def step_all_old(context):
    conditional = [p for p in context.calls.of("UpdateItem") if "ConditionExpression" in p]
    assert conditional and all(p["ReturnValuesOnConditionCheckFailure"] == "ALL_OLD" for p in conditional)
    assert all(p["ConditionExpression"] == "version = :seen" for p in conditional)


@then("each round read its trip 3 times: once per client and once at the end")
def step_three_reads(context):
    assert len(context.calls.of("GetItem")) == 3 * len(context.calls.of("PutItem"))


@then("every GetItem call was a strongly consistent read")
def step_strong(context):
    reads = context.calls.of("GetItem")
    assert reads and all(p.get("ConsistentRead") is True for p in reads), reads[:3]


@then("on every trip the first two reads come before the first accept")
def step_reads_first(context):
    rounds, current = [], {}
    for op, p in context.calls.made:
        trip = (p.get("Item") or p.get("Key") or {}).get("trip_id", {}).get("S")
        if trip is None:
            continue
        if op == "PutItem":
            current[trip] = []
            rounds.append(current[trip])
        else:
            current[trip].append(op)
    assert len(rounds) >= 16, len(rounds)
    for ops in rounds:
        assert ops[:2] == ["GetItem", "GetItem"] and ops[2] == "UpdateItem", ops


@then("each mode ran on {n:d} trips of its own turn, none used twice in a mode")
def step_fresh_trips(context, n):
    trips = [p["Item"]["trip_id"]["S"] for p in context.calls.of("PutItem")]
    assert len(trips) == 2 * n + 1, len(trips)
    assert len(set(trips[:n])) == n and len(set(trips[n:2 * n])) == n, trips


@then("the recipe asked for the table {n:d} times, and looked whether it was gone in between")
def step_created_twice(context, n):
    names = context.calls.names()
    creates = [i for i, op in enumerate(names) if op == "CreateTable"]
    assert len(creates) == n, names[:8]
    assert "DescribeTable" in names[creates[0]:creates[1]], names[:8]


@given('a trip "{trip}" that waits, read by driver "{driver}" at version {version:d}')
def step_waiting_trip(context, trip, driver, version):
    import trips

    table = trips.table_name(support.PREFIX)
    trips.create_table(support.ddb(), table, poll_s=0)
    trips.reset_trip(support.ddb(), table, trip)
    assert trips.read_trip(support.ddb(), table, trip)["version"] == version
    context.answers = []
    context.calls.made.clear()


@when('driver "{driver}" sends the safe accept for "{trip}" with version {version:d}')
def step_safe_accept(context, driver, trip, version):
    import trips

    context.answers.append(support.run(context, trips.accept_safe, support.ddb(), trips.table_name(support.PREFIX),
                                       trip, driver, version))
    assert context.error is None, context.error


@then("both answers say accepted")
def step_both_accepted(context):
    assert len(context.answers) == 2 and all(a["accepted"] is True for a in context.answers), context.answers


@then('the last answer says refused, held by "{driver}"')
def step_last_refused(context, driver):
    assert context.answers[-1]["accepted"] is False and context.answers[-1]["held_by"] == driver, context.answers


@then('trip "{trip}" holds driver "{driver}" at version {version:d}')
def step_trip_holds(context, trip, driver, version):
    import trips

    seen = trips.read_trip(support.ddb(), trips.table_name(support.PREFIX), trip)
    assert seen["driver_id"] == driver and seen["version"] == version and seen["status"] == "accepted", seen


@then("{n:d} of the {total:d} writes was refused by DynamoDB")
def step_refused_writes(context, n, total):
    writes = context.calls.of("UpdateItem")
    assert len(writes) == total, len(writes)
    assert context.calls.refused == n, context.calls.refused


# ---- the retried write

def _tips(context):
    return [p for p in context.calls.of("UpdateItem") if p.get("UpdateExpression") == "ADD tip_total :a"]


def _tip_on(trip):
    item = support.ddb().get_item(TableName=f"{support.PREFIX}-race-trips", Key={"trip_id": {"S": trip}},
                                  ConsistentRead=True).get("Item")
    return None if item is None else int(item["tip_total"]["N"])


@then('the naive write was sent {n:d} times and the tip on "{trip}" is {tip:d}')
def step_naive(context, n, trip, tip):
    assert len(_tips(context)) == n and _tip_on(trip) == tip, (len(_tips(context)), _tip_on(trip))


@then("the naive write was sent {n:d} times")
def step_naive_n(context, n):
    assert len(_tips(context)) == n, len(_tips(context))


@then('the transaction was sent {n:d} times, both with the same request id, and the tip on "{trip}" is {tip:d}')
def step_transaction(context, n, trip, tip):
    sent = context.calls.of("TransactWriteItems")
    ids = {p["TransactItems"][0]["Put"]["Item"]["request_id"]["S"] for p in sent}
    assert len(sent) == n and len(ids) == 1 and _tip_on(trip) == tip, (len(sent), ids, _tip_on(trip))


def _markers():
    return support.ddb().scan(TableName=f"{support.PREFIX}-race-requests", ConsistentRead=True)["Items"]


@then('the requests table holds {n:d} marker, for trip "{trip}" and amount {amount:d}')
def step_marker(context, n, trip, amount):
    markers = _markers()
    assert len(markers) == n and markers[0]["trip_id"]["S"] == trip and markers[0]["amount"]["N"] == str(amount)


@then("the requests table holds 0 markers")
def step_no_markers(context):
    assert _markers() == []


@then('the call raises "{name}"')
def step_raises(context, name):
    assert type(context.error).__name__ == name, (context.error, context.printed)


@then('trip "{trip}" does not exist')
def step_no_trip(context, trip):
    assert _tip_on(trip) is None


@then('the run stops with the error "{text}"')
def step_error(context, text):
    assert context.error is not None and text in str(context.error), (context.error, context.printed)


@then('the recipe waited "{seconds}" seconds after the attempts')
def step_waits(context, seconds):
    assert [str(s) for s in context.sleeps] == _lst(seconds), context.sleeps


@then('the marker\'s "expires_at" is {days:d} days after now, within a minute')
def step_expires(context, days):
    (marker,) = _markers()
    assert abs(int(marker["expires_at"]["N"]) - (time.time() + days * 86400)) < 60, marker


@then('time to live is on for "{attribute}" in the table "{table}"')
def step_ttl(context, attribute, table):
    ttl = support.ddb().describe_time_to_live(TableName=table)["TimeToLiveDescription"]
    assert ttl == {"TimeToLiveStatus": "ENABLED", "AttributeName": attribute}, ttl


@then("every TransactWriteItems call has its marker's request id as ClientRequestToken")
def step_token(context):
    sent = context.calls.of("TransactWriteItems")
    assert sent and all(p["ClientRequestToken"] == p["TransactItems"][0]["Put"]["Item"]["request_id"]["S"] for p in sent)

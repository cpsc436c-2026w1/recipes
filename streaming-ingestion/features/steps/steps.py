"""Steps for the four features. Each When runs an entry point of the recipe: a Lambda handler (consumer,
windows, fan-out consumer), main/run.py, the two extensions' run.py, or clean_up.py."""

from __future__ import annotations

import base64
import datetime as dt
import io
import json
import os
import re
import time
import zipfile

import boto3

import support
from behave import given, then, when
from support import ROOT


def _lst(text: str) -> list[str]:
    return [t.strip() for t in text.split(",") if t.strip()]


# ---- settings and stand-in rules

@given('the setting {name} is "{value}"')
def step_env(context, name, value):
    if value == "not set":
        os.environ.pop(name, None)
    else:
        os.environ[name] = value


@given("no AWS credentials and no Region in the environment")
def step_no_credentials(context):
    for name in [n for n in os.environ if n.startswith("AWS_") and n != "AWS_EC2_METADATA_DISABLED"]:
        os.environ.pop(name)


@given('AWS refuses every "{operation}" call of "{service}" with "{code}"')
def step_refuse(context, operation, service, code):
    context.calls.refuse(operation, code)


# ---- the consumer

def _kinesis_event(rows) -> dict:
    records = []
    for r in rows:
        update = {k: r[k] for k in ("update_id", "driver_id", "event_time") if r.get(k)}
        if r.get("zone_id"):
            update["zone_id"] = int(r["zone_id"])
        records.append({"eventSource": "aws:kinesis", "eventID": f"shardId-000000000000:{r['seq']}", "kinesis": {
            "partitionKey": r.get("driver_id", "d"), "sequenceNumber": r["seq"],
            "data": base64.b64encode(json.dumps(update).encode()).decode(),
            "approximateArrivalTimestamp": float(r["entered at"])}})
    return {"Records": records}


@given('the table "{table}" exists and the function is told its name')
def step_table(context, table):
    import infra

    infra.create_table(support.client("dynamodb"), table, "update_id", poll_s=0)
    os.environ["TABLE"] = table
    context.table_name = table
    context.calls.clear_made()


@given("the stream records")
def step_records(context):
    context.event = _kinesis_event([dict(zip(r.headings, r.cells)) for r in context.table])


@when("the function receives them at {at:f} seconds")
@when("the function receives them at {at:d} seconds")
def step_consumer(context, at):
    import consumer

    consumer._client = None                 # the function makes its client on first use, as in a new environment
    support.run(context, consumer.handler, context.event, None, clock=lambda: at)
    assert context.error is None, context.error


@then("the function reports no failed record")
def step_no_failed(context):
    assert context.result == {"batchItemFailures": []}, context.result


@then('the function reports the record with sequence number "{seq}" as failed')
def step_failed(context, seq):
    assert context.result == {"batchItemFailures": [{"itemIdentifier": seq}]}, context.result


@then("the table holds {n:d} updates")
def step_holds(context, n):
    assert len(support.items(context.table_name)) == n, support.items(context.table_name)


@then('update "{uid}" has event_ms {e:d}, entered_ms {en:d}, arrival_ms {a:d} and lag_ms {lag:d}')
def step_times(context, uid, e, en, a, lag):
    (item,) = [i for i in support.items(context.table_name) if i["update_id"] == uid]
    found = tuple(int(item[k]) for k in ("event_ms", "entered_ms", "arrival_ms", "lag_ms"))
    assert found == (e, en, a, lag), found


@then("the function's last log line counts {stored:d} stored and {repeat:d} repeat")
def step_log_counts(context, stored, repeat):
    assert json.loads(context.printed.strip().splitlines()[-1]) == {"stored": stored, "repeat": repeat}


# ---- running the scripts

@when('{which} runs with "{args}"')
def step_run(context, which, args):
    if "--target local" in args and getattr(context, "runs", None):
        # A local run starts its own moto and expects it empty; inside the scenario's moto a second local run
        # would find the first one's tables. Start from an empty account, as two runs on a laptop do.
        context.moto.stop()
        context.moto.start()
    support.run_script(context, which, args)
    context.runs = getattr(context, "runs", []) + [context.printed]


@when("clean_up.py runs")
def step_clean_up(context):
    support.run_clean_up(context, "--yes")      # a bare run only lists (the clean-up contract)
    context.runs = getattr(context, "runs", []) + [context.printed]


@when("clean_up.py --dry-run runs")
def step_clean_up_dry(context):
    context.before_dry_run = support.inventory()
    support.run_clean_up(context, "--dry-run")


@then("the account holds what it held before")
def step_unchanged(context):
    assert support.inventory() == context.before_dry_run


@then('no call was made to delete anything')
def step_no_delete(context):
    deletes = [pair for pair in context.calls.pairs() if pair[1].startswith(("Delete", "Unsubscribe"))]
    assert not deletes, deletes


@then("the exit code is {code:d}")
def step_code(context, code):
    assert context.exit_code == code and (context.error is None or code != 0), (context.exit_code, context.error,
                                                                               context.printed[-1500:])


@then('the run stops with the error "{text}"')
def step_error(context, text):
    assert context.error is not None and text in str(context.error), (context.error, context.printed[-800:])


@then('the output holds "{text}"')
def step_out(context, text):
    assert text in context.printed, context.printed[-2500:]


@then('no printed line starts with "{start}"')
def step_no_line(context, start):
    assert not [l for l in context.printed.splitlines() if l.startswith(start)], context.printed


@then('at least {n:d} printed line starts with "{start}"')
def step_lines_at_least(context, n, start):
    assert len([l for l in context.printed.splitlines() if l.startswith(start)]) >= n, context.printed[-2500:]


@then('{n:d} printed lines hold "{text}"')
def step_lines_hold(context, n, text):
    found = [l for l in context.printed.splitlines() if text in l]
    assert len(found) == n, found


@then('the second run printed no line that starts with "{a}" or "{b}"')
def step_second_run(context, a, b):
    second = context.runs[-1][len(context.runs[-2]):]
    assert second.strip() and not [l for l in second.splitlines() if l.startswith((a, b))], second


# ---- the producer

def _put_records(context) -> list[dict]:
    return context.calls.of("PutRecords", "kinesis")


@then('every record put on the stream "{stream}" has its driver_id as partition key')
def step_partition_key(context, stream):
    calls = _put_records(context)
    assert calls and all(c["StreamName"] == stream for c in calls)
    for c in calls:
        for r in c["Records"]:
            assert r["PartitionKey"] == json.loads(r["Data"])["driver_id"], r


@then("every record carries an update_id, an event_time and a sent_time at or after it")
def step_stamped(context):
    for c in _put_records(context):
        for r in c["Records"]:
            u = json.loads(r["Data"])
            assert u["update_id"] and u["event_time"] <= u["sent_time"], u


@then('the PutRecords calls carry "{counts}" records')
def step_put_counts(context, counts):
    assert [len(c["Records"]) for c in _put_records(context)] == [int(n) for n in _lst(counts)]


def _refuse_first(n: int, calls_left: list):
    """The stream refuses the first n records of a call (while calls_left is not 0): they do not reach the stream,
    and the answer lists them as failed, in their places, as PutRecords does."""
    refused = [0]

    def before(params):
        refused[0] = 0 if calls_left[0] == 0 else min(n, len(params["Records"]))
        if calls_left[0] > 0:
            calls_left[0] -= 1
        return {**params, "Records": params["Records"][refused[0]:]} if refused[0] < len(params["Records"]) else None

    def change(answer, params):
        k = refused[0]
        if not k:
            return answer
        failed = [{"ErrorCode": "ProvisionedThroughputExceededException", "ErrorMessage": "Rate exceeded"}] * k
        return {**answer, "Records": failed + answer["Records"], "FailedRecordCount": answer.get("FailedRecordCount", 0) + k}
    return change, before


@given("the stream refuses the first {n:d} records of its first {calls:d} calls")
def step_refuse_once(context, n, calls):
    change, before = _refuse_first(n, [calls])
    context.calls.change("PutRecords", change, before=before)


@given("the stream refuses the first {n:d} records of every call")
def step_refuse_always(context, n):
    change, before = _refuse_first(n, [-1])
    context.calls.change("PutRecords", change, before=before)


@given("the producer's random waits come out at the top of their range")
def step_uniform_top(context):
    """random.uniform(a, b) returns b and notes its range, so the limits can be checked exactly."""
    import random

    real, context.uniform_ranges = random.uniform, []

    def top(a, b):
        context.uniform_ranges.append((a, b))
        return b
    random.uniform = top
    context.add_cleanup(setattr, random, "uniform", real)


@then('the producer waited {n:d} times between its calls, each a random time from 0 up to the limits "{limits}" seconds')
def step_producer_waits(context, n, limits):
    limits = [float(x) for x in _lst(limits)]
    waits = [s for s in context.sleeps if 0 <= s <= 2]
    assert waits == limits and len(limits) == n, context.sleeps                    # the top of each range
    assert context.uniform_ranges == [(0, lim) for lim in limits], context.uniform_ranges   # drawn from 0 up


# ---- the report

@then('the report has a median and a p95 for "{a}", "{b}" and "{c}"')
def step_report_parts(context, a, b, c):
    assert sorted(support.lag_rows(context.printed)) == sorted([a, b, c]), support.lag_rows(context.printed)


@then("in every part the median is at most the p95")
def step_median_p95(context):
    assert all(m <= p for m, p in support.lag_rows(context.printed).values())


@given("a file of {n:d} updates whose event times are 1, 2, ... {m:d} minutes before now")
def step_file(context, n, m):
    now = dt.datetime.now(dt.UTC)
    context.file = context.tmp / "updates.jsonl"
    with open(context.file, "w") as f:
        for i in range(1, n + 1):
            when_ = (now - dt.timedelta(minutes=i)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            f.write(json.dumps({"update_id": f"file-{i}", "driver_id": f"d-{i % 4}", "zone_id": 161,
                                "event_time": when_}) + "\n")


@given('a file of {n:d} updates in which the update "{twice}" comes twice')
def step_file_twice(context, n, twice):
    context.file = context.tmp / "updates.jsonl"
    now = dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    lines = [json.dumps({"update_id": f"file-{i}", "driver_id": f"d-{i % 4}", "zone_id": 161, "event_time": now})
             for i in range(1, n + 1)]
    lines.append(next(line for line in lines if f'"{twice}"' in line))
    context.file.write_text("\n".join(lines) + "\n")


@when('main/run.py runs with that file and "{args}"')
def step_run_file(context, args):
    support.run_script(context, "main/run.py", f"{args} --from-file {context.file}")


@then('the {stat} of "{part}" is {minutes:d} minutes, within {tolerance:d} seconds')
def step_stat(context, stat, part, minutes, tolerance):
    median, p95 = support.lag_rows(context.printed)[part]
    value = median if stat == "median" else p95
    assert 0 <= value - minutes * 60 <= tolerance, (stat, value)


# ---- what a run on AWS created

def _nothing_left() -> bool:
    return not any(support.inventory().values())


@then("the account has nothing of the recipe left")
def step_nothing(context):
    assert _nothing_left(), support.inventory()


@then('the account has the stream "{stream}", the table "{table}", the function "{function}", the queue "{queue}" and the role "{role}"')
def step_kept(context, stream, table, function, queue, role):
    found = support.inventory()
    assert (found["streams"], found["tables"], found["functions"], found["queues"], found["roles"]) == (
        [stream], [table], [function], [queue], [role]), found
    assert found["mappings"] == 1


@given('DynamoDB refuses to store the updates "{ids}"')
def step_refuse_updates(context, ids):
    wanted = set(_lst(ids))
    context.calls.refuse("PutItem", "ProvisionedThroughputExceededException",
                         when=lambda p: p.get("Item", {}).get("update_id", {}).get("S") in wanted)


@then("the recipe looked at the table every {seconds:d} seconds while it waited")
def step_poll_interval(context, seconds):
    waits = [s for s in context.sleeps if s == seconds]
    assert len(waits) == 4, context.sleeps            # 20 s of waiting, in steps of 5


@given('the function "{name}" fails on every batch')
def step_stop_function(context, name):
    support.STOPPED.add(name)
    context.add_cleanup(support.STOPPED.discard, name)


@given('the stream "{stream}" is in the DELETING state until the recipe has waited for it')
def step_stream_deleting(context, stream):
    context.calls.refuse("CreateStream", "ResourceInUseException", times=1)
    context.calls.answer("DescribeStreamSummary",
                         {"StreamDescriptionSummary": {"StreamName": stream, "StreamStatus": "DELETING"}}, times=1)


@then("the recipe asked for the stream {n:d} times, and looked whether it was gone in between")
def step_stream_twice(context, n):
    names = context.calls.names()
    creates = [i for i, op in enumerate(names) if op == "CreateStream"]
    assert len(creates) == n and "DescribeStreamSummary" in names[creates[0]:creates[1]], names[:8]


@then("no call was made to Lambda, IAM or SQS")
def step_local_services(context):
    assert not {"lambda", "iam", "sqs"} & set(context.calls.services), set(context.calls.services)


@then('the stream "{stream}" was created with {n:d} shard, in provisioned mode')
def step_stream(context, stream, n):
    (made,) = context.calls.of("CreateStream")
    assert made == {"StreamName": stream, "ShardCount": n, "StreamModeDetails": {"StreamMode": "PROVISIONED"}}, made


def _mapping(context, function: str | None = None) -> dict:
    made = [m for m in context.calls.of("CreateEventSourceMapping") if function in (None, m["FunctionName"])]
    assert len(made) == 1, [m["FunctionName"] for m in context.calls.of("CreateEventSourceMapping")]
    return made[0]


@then('the event source mapping starts at "{start}", with batches of {size:d} records or {window:d} second')
def step_mapping(context, start, size, window):
    m = _mapping(context)
    assert (m["StartingPosition"], m["BatchSize"], m["MaximumBatchingWindowInSeconds"]) == (start, size, window), m
    assert m["FunctionResponseTypes"] == ["ReportBatchItemFailures"]


@then("a failed batch is split and tried again at most {tries:d} times, and records older than {age:d} seconds are given up")
def step_mapping_failures(context, tries, age):
    m = _mapping(context)
    assert m["BisectBatchOnFunctionError"] is True and m["MaximumRetryAttempts"] == tries, m
    assert m["MaximumRecordAgeInSeconds"] == age, m


@then('what Lambda gives up on goes to the queue "{queue}"')
def step_on_failure(context, queue):
    m = _mapping(context)
    assert m["DestinationConfig"] == {"OnFailure": {"Destination": f"arn:aws:sqs:ca-central-1:123456789012:{queue}"}}


def _policy(context, role: str) -> list[dict]:
    (put,) = [p for p in context.calls.of("PutRolePolicy") if p["RoleName"] == role]
    return json.loads(put["PolicyDocument"])["Statement"]


@then('the role "{role}" may read the stream "{stream}", put items into the table "{table}", send to the queue "{queue}" and write the log group "{group}"')
def step_role(context, role, stream, table, queue, group):
    by_service = {}
    for st in _policy(context, role):
        by_service[st["Action"][0].split(":")[0]] = st
    context.role_statements = _policy(context, role)
    assert by_service["kinesis"]["Resource"].endswith(f":stream/{stream}")
    assert all(a.startswith(("kinesis:Get", "kinesis:Describe", "kinesis:List")) for a in by_service["kinesis"]["Action"])
    assert by_service["dynamodb"]["Action"] == ["dynamodb:PutItem"] and by_service["dynamodb"]["Resource"].endswith(f":table/{table}")
    assert by_service["sqs"]["Action"] == ["sqs:SendMessage"] and by_service["sqs"]["Resource"].endswith(f":{queue}")
    assert by_service["logs"]["Resource"].endswith(f":log-group:{group}:*")


@then('no statement of the role\'s policy has "*" as its resource or in an action')
def step_no_star(context):
    for st in context.role_statements:
        resources = st["Resource"] if isinstance(st["Resource"], list) else [st["Resource"]]
        assert "*" not in resources and not any("*" in a for a in st["Action"]), st


@then("the role's policy has no other statement")
def step_four_statements(context):
    assert len(context.role_statements) == 4, context.role_statements


@given('LAMBDA_ROLE_ARN names the role "{name}" of the account')
def step_role_passed(context, name):
    os.environ["LAMBDA_ROLE_ARN"] = support.client("iam").create_role(
        RoleName=name, AssumeRolePolicyDocument="{}")["Role"]["Arn"]
    context.calls.clear_made()


@then("no role was created")
def step_no_role(context):
    assert not {"CreateRole", "PutRolePolicy"} & set(context.calls.names())


@then('the function "{name}" runs as "{arn}"')
def step_runs_as(context, name, arn):
    (made,) = [f for f in context.calls.of("CreateFunction") if f["FunctionName"] == name]
    assert made["Role"] == arn


@then('the function "{name}" was created with "{runtime}" on "{arch}", {memory:d} MB and a timeout of {timeout:d} seconds')
def step_function(context, name, runtime, arch, memory, timeout):
    (made,) = [f for f in context.calls.of("CreateFunction") if f["FunctionName"] == name]
    assert (made["Runtime"], made["Architectures"], made["MemorySize"], made["Timeout"]) == (
        runtime, [arch], memory, timeout), {k: v for k, v in made.items() if k != "Code"}
    context.function = made


@then('its package holds only "{name}", the same bytes as main/consumer.py')
def step_package(context, name):
    with zipfile.ZipFile(io.BytesIO(context.function["Code"]["ZipFile"])) as z:
        assert z.namelist() == [name] and z.read(name) == (ROOT / "main" / "consumer.py").read_bytes()


@then("the function ran {n:d} time with a batch of {size:d} records, from that package")
def step_function_runs(context, n, size):
    runs = [r for r in support.FUNCTION_RUNS if r["function"] == context.function["FunctionName"]]
    assert len(runs) == n and all(r.get("package") == ["consumer.py"] for r in runs), runs[:2]
    assert [len(r["event"]["Records"]) for r in runs] == [size] * n, [len(r["event"]["Records"]) for r in runs]


@then('the stand-in gave up on the records from sequence number "{first}" to "{last}" and sent their place to the queue "{queue}"')
def step_given_up(context, first, last, queue):
    # The run has removed the queue by now; the stand-in notes what it sent there.
    found = [(g["startSequenceNumber"], g["endSequenceNumber"], g.get("queue")) for g in support.GIVEN_UP]
    assert found == [(first, last, queue)], support.GIVEN_UP


@then('every stream, table, function, queue, role and log group of the account starts with "{start}"')
def step_prefix(context, start):
    assert context.exit_code == 0, context.printed + str(context.error)
    found = support.inventory()
    names = found["streams"] + found["tables"] + found["functions"] + found["queues"] + found["roles"]
    assert len(names) == 5 and all(n.startswith(start) for n in names), names
    assert all(g.startswith(f"/aws/lambda/{start}") for g in found["log groups"]), found["log groups"]


@then('every call went to the Region "{region}"')
def step_regions(context, region):
    found = {r for r, s in zip(context.calls.regions, context.calls.services) if s not in ("iam", "sts")}
    assert found == {region}, found


# ---- clean-up

@given('everything the main scenario and both extensions create exists for PREFIX "{prefix}"')
def step_everything(context, prefix):
    os.environ["PREFIX"] = prefix
    for which, args in (("main/run.py", "--target aws --updates 10 --rate 0 --keep"),
                        ("windowed aggregates", "--target aws --keep"),
                        ("fan-out", "--target aws --updates 10 --rate 0 --stopped-for 0 --keep")):
        support.run_script(context, which, args)
        assert context.exit_code == 0, (which, context.printed[-1500:], context.error)
    found = support.inventory()
    assert len(found["functions"]) == 4 and len(found["tables"]) == 4 and len(found["queues"]) == 6, found
    context.calls.clear()
    context.printed, context.sleeps, context.runs = "", [], []


@given('the stream "{stream}", the table "{table}" and the queue "{queue}" of other owners exist')
def step_others(context, stream, table, queue):
    import infra

    infra.create_stream(support.client("kinesis"), stream, poll_s=0)
    infra.create_table(support.client("dynamodb"), table, "id", poll_s=0)
    support.client("sqs").create_queue(QueueName=queue)
    context.calls.clear()


@then('the account has exactly the stream "{stream}", the table "{table}" and the queue "{queue}" left')
def step_others_left(context, stream, table, queue):
    found = support.inventory()
    assert (found["streams"], found["tables"], found["queues"]) == ([stream], [table], [queue]), found
    assert not found["functions"] and not found["roles"] and not found["topics"], found


@given("the stream, the tables and the event source mappings are being deleted")
def step_being_deleted(context):
    context.calls.change("ListEventSourceMappings", lambda r, p: {**r, "EventSourceMappings": [
        {**m, "State": "Deleting"} for m in r["EventSourceMappings"]]})
    context.calls.answer("DescribeTable", {"Table": {"TableStatus": "DELETING"}})
    context.calls.answer("DescribeStreamSummary", {"StreamDescriptionSummary": {"StreamStatus": "DELETING"}})


@then("no call was made to delete the stream, a table or a mapping again")
def step_not_again(context):
    again = {("kinesis", "DeleteStream"), ("dynamodb", "DeleteTable"), ("lambda", "DeleteEventSourceMapping")}
    assert not again & set(context.calls.pairs()), again & set(context.calls.pairs())


@then('the queues "{queues}" keep their messages {days:d} days')
def step_queue_days(context, queues, days):
    sqs = support.client("sqs")
    for queue in _lst(queues):
        url = sqs.get_queue_url(QueueName=queue)["QueueUrl"]
        found = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["MessageRetentionPeriod"])["Attributes"]
        assert int(found["MessageRetentionPeriod"]) == days * 86400, (queue, found)


@then('the log groups "{groups}" were made before their functions and keep their lines {days:d} days')
def step_log_groups(context, groups, days):
    names = context.calls.names()
    for group in _lst(groups):
        function = group.rsplit("/", 1)[1]
        made = [i for i, (op, p) in enumerate(context.calls.made)
                if op == "CreateLogGroup" and p["logGroupName"] == group]
        created = [i for i, (op, p) in enumerate(context.calls.made)
                   if op == "CreateFunction" and p["FunctionName"] == function]
        assert made and created and made[0] < created[0], (group, names)
        (found,) = support.client("logs").describe_log_groups(logGroupNamePrefix=group)["logGroups"]
        assert found["retentionInDays"] == days, found


# ---- windowed aggregates

def _windows(printed: str) -> list[dict]:
    rows = []
    for m in re.finditer(r"^\s*(\d+)\s+(\S+Z)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(final|open)$", printed, flags=re.M):
        rows.append({"zone": int(m.group(1)), "start": m.group(2), "count": int(m.group(3)), "dropped": int(m.group(4)),
                     "corrections": int(m.group(5)), "truth": int(m.group(6)), "state": m.group(7)})
    return rows


def _last_run(context) -> str:
    runs = context.runs
    return runs[-1][len(runs[-2]):] if len(runs) > 1 else runs[-1]


@then('every printed window starts on a 5-minute boundary and is "final" or "open"')
def step_window_rows(context):
    rows = _windows(_last_run(context))
    assert len(rows) >= 20, len(rows)
    for r in rows:
        start = dt.datetime.fromisoformat(r["start"].replace("Z", "+00:00"))
        assert start.minute % 5 == 0 and start.second == 0, r


@then("the total counted plus the total late and dropped is {n:d}, with more than 0 dropped")
def step_totals_drop(context, n):
    rows = _windows(_last_run(context))
    dropped = sum(r["dropped"] for r in rows)
    assert sum(r["count"] for r in rows) + dropped == n and dropped > 0, dropped


@then("the total counted is {n:d}, with more than 0 corrections and 0 dropped")
def step_totals_correct(context, n):
    rows = _windows(_last_run(context))
    assert sum(r["count"] for r in rows) == n and sum(r["corrections"] for r in rows) > 0
    assert sum(r["dropped"] for r in rows) == 0


@then('the windows that end after the newest event time minus {delay:d} seconds are "open", the others "final"')
def step_open_final(context, delay):
    import timeline
    import windows

    newest = max(windows._ms(u["event_time"]) for u in timeline.make_timeline())
    for r in _windows(_last_run(context)):
        end = dt.datetime.fromisoformat(r["start"].replace("Z", "+00:00")).timestamp() * 1000 + windows.WINDOW_MS
        assert r["state"] == ("final" if end <= newest - delay * 1000 else "open"), r


@when("the dropped total is noted")
def step_note_dropped(context):
    context.noted_dropped = sum(r["dropped"] for r in _windows(_last_run(context)))


@then("the dropped total is smaller than the one noted")
def step_fewer_dropped(context):
    now = sum(r["dropped"] for r in _windows(_last_run(context)))
    assert now < context.noted_dropped, (now, context.noted_dropped)


@then('the function "{name}" was created from "{file}" alone, with LATE_RULE "{rule}" and CLOSE_DELAY_S "{delay}"')
def step_window_function(context, name, file, rule, delay):
    (made,) = [f for f in context.calls.of("CreateFunction") if f["FunctionName"] == name]
    with zipfile.ZipFile(io.BytesIO(made["Code"]["ZipFile"])) as z:
        assert z.namelist() == [file]
    env = made["Environment"]["Variables"]
    assert env["LATE_RULE"] == rule and env["CLOSE_DELAY_S"] == delay and env["TABLE"].endswith("-windows"), env


@then('its event source mapping reads the stream "{stream}" in batches of {size:d}')
def step_window_mapping(context, stream, size):
    m = _mapping(context)
    assert m["EventSourceArn"].endswith(f":stream/{stream}") and m["BatchSize"] == size, m


@given('the window table of PREFIX "{prefix}" exists and the window function is told its name')
def step_window_table(context, prefix):
    import infra

    context.table_name = f"{prefix}-windows"
    infra.create_table(support.client("dynamodb"), context.table_name, "pk", poll_s=0)
    os.environ["TABLE"] = context.table_name


@given("{n:d} updates of one zone, {gap:d} seconds apart")
def step_zone_updates(context, n, gap):
    start = 1_800_000_000
    rows = [{"seq": str(100 + i), "update_id": f"w-{i}", "driver_id": "d-1", "zone_id": "161",
             "event_time": dt.datetime.fromtimestamp(start + i * gap, dt.UTC).isoformat().replace("+00:00", "Z"),
             "entered at": str(start + i * gap)} for i in range(n)]
    context.event = _kinesis_event(rows)


@when("the window function receives them, and then receives them again")
def step_windows_twice(context):
    import windows

    windows._client = None
    for _ in range(2):
        support.run(context, windows.handler, context.event, None)
        assert context.error is None and context.result == {"batchItemFailures": []}, (context.error, context.result)


@then("the windows hold {n:d} updates in all")
def step_windows_total(context, n):
    counts = [int(i.get("count", 0)) for i in support.items(context.table_name) if i["pk"].startswith("window#")]
    assert sum(counts) == n, counts


@then("the function's last log line counts {n:d} repeats")
def step_repeats(context, n):
    assert json.loads(context.printed.strip().splitlines()[-1]).get("repeat") == n, context.printed[-300:]


@then('time to live is on for "{attribute}" in the table "{table}"')
def step_ttl(context, attribute, table):
    ttl = support.client("dynamodb").describe_time_to_live(TableName=table)["TimeToLiveDescription"]
    assert ttl == {"TimeToLiveStatus": "ENABLED", "AttributeName": attribute}, ttl
    context.table_name = table


@when("the window function receives them at {at:d} seconds")
def step_windows_at(context, at):
    import windows

    windows._client = None
    support.run(context, windows.handler, context.event, None, clock=lambda: at)
    assert context.error is None and context.result == {"batchItemFailures": []}, (context.error, context.result)


@then('each of the {n:d} markers has "expires_at" {seconds:d} seconds after {at:d}')
def step_markers(context, n, seconds, at):
    markers = [i for i in support.items(context.table_name) if i["pk"].startswith("seen#")]
    assert len(markers) == n and all(int(m["expires_at"]) == at + seconds for m in markers), markers[:2]


# ---- fan-out

@then('the report of "{a}" and of "{b}" each say "{text}"')
def step_two_reports(context, a, b, text):
    for name in (a, b):
        block = context.printed.split(f"\n{name}\n", 1)[1]
        assert block.startswith(text), block[:80]


@then("each consumer's mapping takes batches of {size:d}, reports failed messages and runs at most {copies:d} copies")
def step_fanout_mappings(context, size, copies):
    made = context.calls.of("CreateEventSourceMapping")
    assert len(made) == 2
    for m in made:
        assert m["BatchSize"] == size and m["FunctionResponseTypes"] == ["ReportBatchItemFailures"], m
        assert m["ScalingConfig"] == {"MaximumConcurrency": copies}, m


@then('the mapping of "{function}" was created stopped and started after the updates were published')
def step_history_mapping(context, function):
    assert _mapping(context, function)["Enabled"] is False
    names = context.calls.names()
    started = [i for i, (op, p) in enumerate(context.calls.made) if op == "UpdateEventSourceMapping" and p.get("Enabled")]
    published = [i for i, op in enumerate(names) if op == "PublishBatch"]
    assert len(started) == 1 and published and started[0] > published[-1], (started, published)


def _queue_attributes(name: str) -> dict:
    sqs = support.client("sqs")
    return sqs.get_queue_attributes(QueueUrl=sqs.get_queue_url(QueueName=name)["QueueUrl"],
                                    AttributeNames=["All"])["Attributes"]


@then('the policy of the queues "{queues}" lets only "{service}" send, and only from the topic "{topic}"')
def step_queue_policy(context, queues, service, topic):
    for q in _lst(queues):
        (st,) = json.loads(_queue_attributes(q)["Policy"])["Statement"]
        assert st["Principal"] == {"Service": service} and st["Action"] == "sqs:SendMessage", st
        assert st["Condition"]["ArnEquals"]["aws:SourceArn"].endswith(f":{topic}") and st["Resource"].endswith(f":{q}")


@then("both queues are subscribed to the topic with raw message delivery")
def step_subscriptions(context):
    made = context.calls.of("Subscribe")
    assert len(made) == 2 and all(s["Protocol"] == "sqs" and s["Attributes"] == {"RawMessageDelivery": "true"}
                                  for s in made), made


@then('the queues "{queues}" move a message to their "-dlq" queue after {n:d} receives')
def step_redrive(context, queues, n):
    for q in _lst(queues):
        policy = json.loads(_queue_attributes(q)["RedrivePolicy"])
        assert policy["deadLetterTargetArn"].endswith(f":{q}-dlq") and int(policy["maxReceiveCount"]) == n, policy


@then("their visibility timeout is {seconds:d} seconds, {times:d} times the function's timeout of {timeout:d} seconds")
def step_visibility(context, seconds, times, timeout):
    for q in ("recipe-ana-dashboard", "recipe-ana-history"):
        assert int(_queue_attributes(q)["VisibilityTimeout"]) == seconds == times * timeout
    assert all(f["Timeout"] == timeout for f in context.calls.of("CreateFunction"))


@given('the table "{table}" exists and the fan-out function is told its name')
def step_fanout_table(context, table):
    step_table(context, table)


@given("{n:d} queue messages, each one update")
def step_messages(context, n):
    context.event = {"Records": [{
        "messageId": f"m-{i}", "receiptHandle": f"h-{i}", "eventSource": "aws:sqs",
        "attributes": {"SentTimestamp": "1800000000500"},
        "body": json.dumps({"update_id": f"u-{i}", "driver_id": "d-1", "zone_id": 161,
                            "event_time": "2027-01-15T08:00:00.000Z"})} for i in range(1, n + 1)]}


@given("message {n:d} is not JSON")
def step_bad_message(context, n):
    context.event["Records"][n - 1]["body"] = "{not json"


def _fanout(context, at: float):
    import fanout_consumer

    fanout_consumer._client = None
    support.run(context, fanout_consumer.handler, context.event, None, clock=lambda: at)
    assert context.error is None, context.error


@when("the fan-out function receives them at {a:d} seconds, and then again at {b:d} seconds")
def step_fanout_twice(context, a, b):
    _fanout(context, a)
    _fanout(context, b)


@when("the fan-out function receives them at {a:d} seconds")
def step_fanout_once(context, a):
    _fanout(context, a)


@then('the table "{table}" holds {n:d} updates, each with arrival_ms {ms:d}')
def step_fanout_stored(context, table, n, ms):
    found = support.items(table)
    assert len(found) == n and all(int(i["arrival_ms"]) == ms for i in found), found


@then("the fan-out function reports message {n:d} as failed")
def step_fanout_failed(context, n):
    assert context.result == {"batchItemFailures": [{"itemIdentifier": f"m-{n}"}]}, context.result


@then('the table "{table}" holds {n:d} updates')
def step_table_holds(context, table, n):
    assert len(support.items(table)) == n

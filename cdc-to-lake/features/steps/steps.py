"""Steps for the five features. Each When runs an entry point of the recipe: the Lambda handler, `python -m
main.run`, clean_up.py, or one of the extension scripts."""

from __future__ import annotations

import datetime as dt
import importlib
import io
import json
import os
import re
import zipfile

import boto3

import support
from behave import given, then, when
from support import ROOT


def _lst(text: str) -> list[str]:
    return [t.strip() for t in text.split(",") if t.strip()]


ENTRY = {"main.run": "main.run", "lag.py": "lag", "rollback.py": "rollback", "schema_change.py": "schema_change",
         "maintenance.py": "maintenance", "clean_up.py": "clean_up"}
CHANGES = [
    {"trip_id": "t-001", "status": "claimed", "pickup_zone": "Kitsilano", "fare_quote": "18.5", "driver_id": "d-42",
     "change_time": "2026-11-13 04:28:56.000000"},
    {"trip_id": "t-002", "status": "requested", "pickup_zone": "Downtown", "fare_quote": "12.0", "driver_id": None,
     "change_time": "2026-11-13 04:28:55.000000"}]
FRESH = [{"changes": 5, "trips": 3, "last_change": "2026-11-13 04:28:56.000000", "max_change_to_arrival_s": 1}]


def _run_entry(context, name: str, args: str):
    module = importlib.import_module(ENTRY[name])
    with support.no_waits(context):
        support.run(context, module.main, argv=args.split())
    context.runs = getattr(context, "runs", []) + [context.printed]


# ---- set-up: environment

@given('the setting {name} is "{value}"')
def step_env(context, name, value):
    assert name in ("PREFIX", "AWS_REGION", "BUFFER_SECONDS", "LAG_LIMIT_SECONDS"), name
    if value == "not set":
        os.environ.pop(name, None)
    else:
        os.environ[name] = value


@given("no AWS credentials and no Region in the environment")
def step_no_credentials(context):
    for name in [n for n in os.environ if n.startswith("AWS_") and n != "AWS_EC2_METADATA_DISABLED"]:
        os.environ.pop(name)


@given('AWS refuses every "{operation}" call with "{code}"')
def step_refuse(context, operation, code):
    context.calls.refuse(operation, code)


# ---- the function

@given('the function\'s Firehose stream is "{stream}"')
def step_stream(context, stream):
    os.environ["DELIVERY_STREAM"] = stream


@given("the stream records")
def step_records(context):
    context.records = [support.stream_record(r["op"], r["trip"], r["seq"], int(r["at"]),
                                             {k: r[k] for k in ("status", "pickup_zone", "fare_quote", "driver_id")})
                       for r in context.table]


@given("{n:d} stream records, one per trip")
def step_many_records(context, n):
    context.records = [support.stream_record("INSERT", f"t-{i:04d}", str(i), 1_800_000_000,
                                             {"status": "requested", "pickup_zone": "Downtown", "fare_quote": "10"})
                       for i in range(1, n + 1)]


@given('Firehose rejects the records with seq "{seqs}" on its first call')
def step_reject_once(context, seqs):
    wanted = {s.rjust(40, "0") for s in _lst(seqs)}
    context.calls.firehose_rejects = lambda call, row: call == 1 and row["seq"] in wanted


@given('Firehose rejects the records with seq "{seqs}" on every call')
def step_reject_always(context, seqs):
    wanted = {s.rjust(40, "0") for s in _lst(seqs)}
    context.calls.firehose_rejects = lambda call, row: row["seq"] in wanted


@when("the function receives them at {at:f} seconds")
@when("the function receives them at {at:d} seconds")
def step_handler(context, at):
    from main import handler

    with support.no_waits(context):
        support.run(context, handler.handler, {"Records": context.records}, None, clock=lambda: at)
    assert context.error is None, context.error


@then("the function reports no failed record")
def step_no_failure(context):
    assert context.result == {"batchItemFailures": []}, context.result


@then('the function reports the record with seq "{seq}" as the first failed one')
def step_failure(context, seq):
    assert context.result == {"batchItemFailures": [{"itemIdentifier": seq}]}, context.result


@then('{n:d} PutRecordBatch call went to the stream "{stream}", with {rows:d} rows for the trips "{trips}"')
def step_one_call(context, n, stream, rows, trips):
    sent = context.calls.of("PutRecordBatch")
    assert len(sent) == n and sent[0]["DeliveryStreamName"] == stream, sent
    assert [r["trip_id"] for r in context.calls.rows_sent()[0]] == _lst(trips)
    assert all(rec["Data"].endswith(b"\n") for rec in sent[0]["Records"])     # one JSON object per line


@then("row {n:d} is")
def step_row(context, n):
    row = context.calls.rows_sent()[0][n - 1]
    want = context.table[0]
    for name in want.headings:
        expected = want[name]
        if name in ("change_time", "arrival_time"):
            assert row[name] == int(expected), (name, row[name])
        elif name == "fare_quote":
            assert row[name] == float(expected), (name, row[name])
        else:
            assert row[name] == (expected or None), (name, row[name])


@then('row {n:d} has the seq "{seq}" padded with zeros to {width:d} digits')
def step_seq(context, n, seq, width):
    found = context.calls.rows_sent()[0][n - 1]["seq"]
    assert found == seq.rjust(width, "0") and len(found) == width, found


@then('row {n:d} has the number {value:f} as "{name}" and nothing as "{other}"')
def step_typed(context, n, value, name, other):
    row = context.calls.rows_sent()[0][n - 1]
    assert isinstance(row[name], float) and row[name] == value and row[other] is None, row


@then('the PutRecordBatch calls carry "{counts}" rows')
def step_counts(context, counts):
    assert [len(rows) for rows in context.calls.rows_sent()] == [int(c) for c in _lst(counts)]


@then('call {n:d} carries only the records with seq "{seqs}"')
def step_call_rows(context, n, seqs):
    assert [r["seq"].lstrip("0") for r in context.calls.rows_sent()[n - 1]] == _lst(seqs)


@then('the recipe waited "{seconds}" seconds between its calls')
def step_waits(context, seconds):
    assert [str(s) for s in context.sleeps] == _lst(seconds), context.sleeps


# ---- running the scripts

@when('{name} runs with "{args}"')
def step_run(context, name, args):
    _run_entry(context, name, args)


@when('{name} runs with ""')
def step_run_plain(context, name):
    _run_entry(context, name, "")


@then("the exit code is {code:d}")
def step_code(context, code):
    assert context.exit_code == code and (context.error is None or code != 0), (context.exit_code, context.error,
                                                                               context.printed[-1500:])


@then('the output holds "{text}"')
def step_out(context, text):
    assert text in context.printed, context.printed[-2000:]


@then('{n:d} printed line starts with "{start}"')
def step_lines(context, n, start):
    found = [line for line in context.printed.splitlines() if line.startswith(start)]
    assert len(found) == n, context.printed[-2000:]


@then('at least {n:d} printed lines start with "{start}"')
def step_lines_at_least(context, n, start):
    assert len([line for line in context.printed.splitlines() if line.startswith(start)]) >= n, context.printed


def _printed_trips(context) -> dict:
    found = {}
    for line in context.printed.splitlines():
        m = re.match(r"^  (t-\d+)\s+(\S+)\s+(.*?)\s+([\d.]+)\s+driver=(\S+)$", line)
        if m:
            found[m.group(1)] = {"status": m.group(2), "zone": m.group(3), "fare": m.group(4), "driver": m.group(5)}
    return found


@then('the printed trips_current has exactly the trips "{trips}"')
def step_printed_trips(context, trips):
    assert sorted(_printed_trips(context)) == _lst(trips), context.printed


@then('the printed trip "{trip}" has status "{status}" and driver "{driver}"')
def step_printed_trip(context, trip, status, driver):
    found = _printed_trips(context)[trip]
    assert found["status"] == status and found["driver"] == driver, found


@then('the output holds a line "{start}" with a time of today')
def step_line_today(context, start):
    (line,) = [l for l in context.printed.splitlines() if l.startswith(start)]
    when_ = dt.datetime.fromisoformat(line[len(start):].strip())
    assert abs((dt.datetime.now(dt.UTC).replace(tzinfo=None) - when_).total_seconds()) < 3600, line


@then('the output holds a line "{start}" with a number of seconds')
def step_line_seconds(context, start):
    (line,) = [l for l in context.printed.splitlines() if l.startswith(start)]
    assert re.fullmatch(r"\d+ s", line[len(start):].strip()), line


@then("no call was made to Athena, Firehose, Glue, S3, Lambda or IAM")
def step_local_services(context):
    assert set(context.calls.services) <= {"dynamodb", "dynamodbstreams"}, set(context.calls.services)


# ---- the local lake

@given("a local lake and a local Firehose with a buffer of {seconds:d} seconds, on a clock the scenario moves")
def step_local_lake(context, seconds):
    from main.config import Settings
    from main.lake import LocalFirehose, LocalLake

    context.now = [1_800_000_000.0]
    context.lake = LocalLake(context.tmp / "lake", Settings())
    context.firehose = LocalFirehose(context.lake, seconds, clock=lambda: context.now[0])


@when("the function sends them to the local Firehose")
def step_send_local(context):
    from main import handler

    out = handler.handler({"Records": context.records}, None, firehose=context.firehose, stream="local",
                          clock=lambda: context.now[0])
    assert out == {"batchItemFailures": []}, out


@when("{n:d} seconds pass")
@when("{n:d} second passes")
def step_pass(context, n):
    context.now[0] += n
    context.firehose.tick()


@then("the lake returns {n:d} rows for the change log")
def step_lake_rows(context, n):
    assert context.lake.query("SELECT count(*) AS n FROM trip_changes")[0]["n"] == n


@then("the freshness query counts {changes:d} changes for {trips:d} trips")
def step_freshness_counts(context, changes, trips):
    from main.config import sql

    (row,) = context.lake.query(sql("freshness", table="trip_changes"))
    assert (row["changes"], row["trips"]) == (changes, trips), row


@then("trips_current has {n:d} rows")
def step_current_rows(context, n):
    assert len(context.lake.query("SELECT * FROM trips_current")) == n


@then('trips_current shows trip "{trip}" with status "{status}"')
def step_current(context, trip, status):
    (row,) = context.lake.query(f"SELECT * FROM trips_current WHERE trip_id = '{trip}'")
    assert row["status"] == status, row


# ---- the pipeline on AWS

@given("Athena shows the changes of the scenario at its {n:d}rd look at trips_current")
@given("Athena shows the changes of the scenario at its {n:d}st look at trips_current")
def step_athena_changes(context, n):
    context.calls.select_returns("FROM trips_current", CHANGES, after_tries=n)
    context.calls.select_returns("max_change_to_arrival_s", FRESH)


@then('the table "{table}" was created with a stream of new and old images and on-demand capacity')
def step_table(context, table):
    (made,) = context.calls.of("CreateTable")
    assert made["TableName"] == table and made["BillingMode"] == "PAY_PER_REQUEST", made
    assert made["StreamSpecification"] == {"StreamEnabled": True, "StreamViewType": "NEW_AND_OLD_IMAGES"}


@then("the table takes at most {r:d} read and {w:d} write request units per second")
def step_ceiling(context, r, w):
    (made,) = context.calls.of("CreateTable")
    assert made["OnDemandThroughput"] == {"MaxReadRequestUnits": r, "MaxWriteRequestUnits": w}, made


@then('public access is blocked on the bucket "{bucket}" in all four ways')
def step_public_block(context, bucket):
    (block,) = context.calls.of("PutPublicAccessBlock")
    assert block["Bucket"] == bucket and block["PublicAccessBlockConfiguration"] == {
        "BlockPublicAcls": True, "IgnorePublicAcls": True, "BlockPublicPolicy": True, "RestrictPublicBuckets": True}


def _ddl(context) -> str:
    (ddl,) = [q for q in context.calls.queries if "CREATE TABLE" in q]
    return ddl


@then('one Athena statement creates the Iceberg table "{table}" at "{location}", partitioned by "{partition}"')
def step_ddl(context, table, location, partition):
    ddl = _ddl(context)
    assert f"CREATE TABLE IF NOT EXISTS {table} (" in ddl and f"LOCATION '{location}'" in ddl, ddl
    assert f"PARTITIONED BY ({partition})" in ddl and "'table_type' = 'ICEBERG'" in ddl, ddl


@then('one Athena statement creates the view "{view}"')
def step_view(context, view):
    assert len([q for q in context.calls.queries if f"CREATE OR REPLACE VIEW {view} AS" in q]) == 1


@then("the table is created with snapshots kept for {seconds:d} seconds and at least {n:d} of them")
def step_ddl_snapshots(context, seconds, n):
    ddl = _ddl(context)
    assert f"'vacuum_max_snapshot_age_seconds' = '{seconds}'" in ddl and f"'vacuum_min_snapshots_to_keep' = '{n}'" in ddl


@then('the bucket "{bucket}" has one lifecycle rule: objects under "{prefix}" expire after {days:d} days')
def step_lifecycle(context, bucket, prefix, days):
    (put,) = context.calls.of("PutBucketLifecycleConfiguration")
    (rule,) = put["LifecycleConfiguration"]["Rules"]
    assert put["Bucket"] == bucket and rule["Status"] == "Enabled", put
    assert rule["Filter"] == {"Prefix": prefix} and rule["Expiration"] == {"Days": days}, rule
    stored = support.client("s3").get_bucket_lifecycle_configuration(Bucket=bucket)["Rules"]
    assert len(stored) == 1 and stored[0]["Expiration"]["Days"] == days, stored


@given('the bucket "{bucket}" exists and is passed in as BUCKET')
def step_bucket_given(context, bucket):
    support.client("s3").create_bucket(Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": "ca-central-1"})
    os.environ["BUCKET"] = bucket
    context.calls.clear_made()


@then("no lifecycle rule was put on any bucket")
def step_no_lifecycle(context):
    assert "PutBucketLifecycleConfiguration" not in context.calls.names()


def _firehose(context) -> dict:
    (made,) = context.calls.of("CreateDeliveryStream")
    return made


@then('the Firehose stream "{stream}" writes to the table "{table}", append-only')
def step_firehose(context, stream, table):
    made = _firehose(context)
    config = made["IcebergDestinationConfiguration"]
    (dest,) = config["DestinationTableConfigurationList"]
    assert made["DeliveryStreamName"] == stream and made["DeliveryStreamType"] == "DirectPut", made
    assert f"{dest['DestinationDatabaseName']}.{dest['DestinationTableName']}" == table and config["AppendOnly"] is True


@then("the Firehose stream buffers for {seconds:d} seconds")
def step_buffer(context, seconds):
    assert _firehose(context)["IcebergDestinationConfiguration"]["BufferingHints"]["IntervalInSeconds"] == seconds


@then('the function "{name}" was created with "{runtime}", {memory:d} MB, a timeout of {timeout:d} seconds and the handler "{handler}"')
def step_function(context, name, runtime, memory, timeout, handler):
    (made,) = context.calls.of("CreateFunction")
    assert (made["FunctionName"], made["Runtime"], made["MemorySize"], made["Timeout"], made["Handler"]) == (
        name, runtime, memory, timeout, handler), {k: v for k, v in made.items() if k != "Code"}


@then('the function\'s package holds only "{name}", the same bytes as main/handler.py')
def step_package(context, name):
    (made,) = context.calls.of("CreateFunction")
    with zipfile.ZipFile(io.BytesIO(made["Code"]["ZipFile"])) as z:
        assert z.namelist() == [name] and z.read(name) == (ROOT / "main" / "handler.py").read_bytes()


@then('the function is told the stream "{stream}" in DELIVERY_STREAM')
def step_function_env(context, stream):
    (made,) = context.calls.of("CreateFunction")
    assert made["Environment"]["Variables"] == {"DELIVERY_STREAM": stream}


@then('the event source mapping starts at "{start}", takes batches of {size:d} after at most {window:d} seconds, reports partial failures, splits a failed batch and gives up on a batch after {tries:d} retries')
def step_mapping(context, start, size, window, tries):
    (made,) = context.calls.of("CreateEventSourceMapping")
    assert made["StartingPosition"] == start and made["BatchSize"] == size, made
    assert made["MaximumBatchingWindowInSeconds"] == window and made["MaximumRetryAttempts"] == tries, made
    assert made["FunctionResponseTypes"] == ["ReportBatchItemFailures"] and made["BisectBatchOnFunctionError"] is True
    assert made["EventSourceArn"].startswith("arn:aws:dynamodb:") and "/stream/" in made["EventSourceArn"]


@then('what Lambda gives up on goes to the queue "{queue}", which exists')
def step_on_failure(context, queue):
    (made,) = context.calls.of("CreateEventSourceMapping")
    assert made["DestinationConfig"] == {"OnFailure": {"Destination": f"arn:aws:sqs:ca-central-1:123456789012:{queue}"}}
    assert support.inventory()["queues"] == [queue]


@given("its event source mapping has no on-failure destination")
def step_mapping_without_destination(context):
    lam = boto3.client("lambda", region_name="ca-central-1")
    (mapping,) = lam.list_event_source_mappings()["EventSourceMappings"]
    lam.update_event_source_mapping(UUID=mapping["UUID"], DestinationConfig={"OnFailure": {}})
    assert not lam.list_event_source_mappings()["EventSourceMappings"][0]["DestinationConfig"].get("OnFailure")
    context.calls.clear()
    context.calls.select_returns("FROM trips_current", CHANGES)       # what Athena answers the next run
    context.calls.select_returns("max_change_to_arrival_s", FRESH)


@then("no second event source mapping was created")
def step_no_second_mapping(context):
    assert context.calls.of("CreateEventSourceMapping") == []
    assert len(boto3.client("lambda", region_name="ca-central-1").list_event_source_mappings()["EventSourceMappings"]) == 1


@then('the event source mapping sends what Lambda gives up on to the queue "{queue}"')
def step_mapping_destination(context, queue):
    (mapping,) = boto3.client("lambda", region_name="ca-central-1").list_event_source_mappings()["EventSourceMappings"]
    assert mapping["DestinationConfig"] == {"OnFailure": {"Destination": f"arn:aws:sqs:ca-central-1:123456789012:{queue}"}}, mapping


@then('the Lambda role may send only to the queue "{queue}"')
def step_lambda_queue(context, queue):
    statements = _policies(context)["recipe-ana-cdc-lambda-role"]
    (send,) = [s for s in statements if any(a.startswith("sqs:") for a in s["Action"])]
    assert send["Action"] == ["sqs:SendMessage"] and send["Resource"].endswith(f":{queue}"), send


def _policies(context) -> dict:
    return {p["RoleName"]: json.loads(p["PolicyDocument"])["Statement"] for p in context.calls.of("PutRolePolicy")}


@then('the roles "{roles}" were created, each for one service')
def step_roles(context, roles):
    made = {r["RoleName"]: json.loads(r["AssumeRolePolicyDocument"])["Statement"] for r in context.calls.of("CreateRole")}
    assert sorted(made) == sorted(_lst(roles)), sorted(made)
    services = sorted(s[0]["Principal"]["Service"] for s in made.values())
    assert services == ["firehose.amazonaws.com", "lambda.amazonaws.com"] and all(len(s) == 1 for s in made.values())


@then('no statement of a role\'s policy has "*" as its resource or in an action')
def step_no_star(context):
    for role, statements in _policies(context).items():
        for st in statements:
            resources = st["Resource"] if isinstance(st["Resource"], list) else [st["Resource"]]
            assert "*" not in resources and not any("*" in a for a in st["Action"]), (role, st)


@then('the Lambda role may read only the stream of "{table}" and write only to the Firehose stream "{stream}"')
def step_lambda_role(context, table, stream):
    statements = _policies(context)["recipe-ana-cdc-lambda-role"]
    read = next(s for s in statements if any(a.startswith("dynamodb:") for a in s["Action"]))
    write = next(s for s in statements if any(a.startswith("firehose:") for a in s["Action"]))
    assert f":table/{table}/stream/" in read["Resource"], read
    assert write["Action"] == ["firehose:PutRecordBatch"] and write["Resource"].endswith(f":deliverystream/{stream}")
    assert not any(a.startswith(("dynamodb:Put", "dynamodb:Update", "dynamodb:Delete", "s3:")) for s in statements
                   for a in s["Action"])


@then('the Firehose role may use only the bucket "{bucket}" and the Glue database "{database}"')
def step_firehose_role(context, bucket, database):
    statements = _policies(context)["recipe-ana-cdc-firehose-role"]
    s3 = next(s for s in statements if any(a.startswith("s3:") for a in s["Action"]))
    glue = next(s for s in statements if any(a.startswith("glue:") for a in s["Action"]))
    assert s3["Resource"] == [f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*"], s3
    assert all(r.endswith((":catalog", f":database/{database}", f":table/{database}/*")) for r in glue["Resource"])


@given('LAMBDA_ROLE_ARN and FIREHOSE_ROLE_ARN name the roles "{a}" and "{b}" of the account')
def step_roles_passed(context, a, b):
    iam = support.client("iam")
    for name, env in ((a, "LAMBDA_ROLE_ARN"), (b, "FIREHOSE_ROLE_ARN")):
        os.environ[env] = iam.create_role(RoleName=name, AssumeRolePolicyDocument="{}")["Role"]["Arn"]
    context.calls.clear_made()


@then("no role was created")
def step_no_role(context):
    assert "CreateRole" not in context.calls.names() and "PutRolePolicy" not in context.calls.names()


@then('the function runs as "{arn}"')
def step_runs_as(context, arn):
    assert context.calls.of("CreateFunction")[0]["Role"] == arn


@then('Firehose assumes "{arn}"')
def step_assumes(context, arn):
    assert _firehose(context)["IcebergDestinationConfiguration"]["RoleARN"] == arn


@then("Athena was asked for trips_current {n:d} times, {seconds:d} seconds apart")
def step_polls(context, n, seconds):
    looks = [q for q in context.calls.queries if q.startswith("SELECT * FROM trips_current")]
    assert len(looks) == n and context.sleeps.count(seconds) == n - 1, (len(looks), context.sleeps)


@given('the table "{table}" is in the DELETING state until the recipe has waited for it')
def step_table_deleting(context, table):
    context.calls.refuse("CreateTable", "ResourceInUseException", times=1)
    context.calls.answer("DescribeTable", {"Table": {"TableName": table, "TableStatus": "DELETING"}}, times=1)


@then("the recipe asked for the table {n:d} times, and looked whether it was gone in between")
def step_created_twice(context, n):
    names = context.calls.names()
    creates = [i for i, op in enumerate(names) if op == "CreateTable"]
    assert len(creates) == n and "DescribeTable" in names[creates[0]:creates[1]], names[:10]


@given('the Firehose stream "{stream}" answers DELETING {n:d} times and is gone after that')
def step_firehose_deleting(context, stream, n):
    context.calls.answer("DescribeDeliveryStream",
                         {"DeliveryStreamDescription": {"DeliveryStreamName": stream, "DeliveryStreamStatus": "DELETING"}},
                         times=n)


@then("{n:d} CreateDeliveryStream call was made, after {looks:d} looks that found the stream being deleted")
def step_firehose_created(context, n, looks):
    names = context.calls.names()
    assert names.count("CreateDeliveryStream") == n
    assert names[:names.index("CreateDeliveryStream")].count("DescribeDeliveryStream") == looks + 1


@then('the account has exactly the table "{table}", the function "{function}", the Firehose stream "{stream}", the Glue database "{database}" and the bucket "{bucket}"')
def step_account(context, table, function, stream, database, bucket):
    assert context.exit_code == 0, context.printed + str(context.error)
    found = support.inventory()
    assert (found["tables"], found["functions"], found["firehose"], found["glue"], found["buckets"]) == (
        [table], [function], [stream], [database], [bucket]), found


@then('every role of the recipe starts with "{start}"')
def step_role_names(context, start):
    roles = [r["RoleName"] for r in context.calls.of("CreateRole")]
    assert len(roles) == 2 and all(r.startswith(start) for r in roles), roles


@then('every call went to the Region "{region}"')
def step_regions(context, region):
    found = {r for r, s in zip(context.calls.regions, context.calls.services) if s not in ("iam", "sts")}
    assert found == {region}, found


@then('the bucket was created in the Region "{region}"')
def step_bucket_region(context, region):
    (made,) = context.calls.of("CreateBucket")
    assert made["CreateBucketConfiguration"] == {"LocationConstraint": region}


# ---- clean-up

def _probe_row(query: str):
    return [{"change_time": (dt.datetime.now(dt.UTC) - dt.timedelta(seconds=91)).strftime("%Y-%m-%d %H:%M:%S.000000")}]


@given("the pipeline of a run with --target aws exists")
def step_pipeline(context):
    context.calls.select_returns("FROM trips_current", CHANGES)
    context.calls.select_returns("max_change_to_arrival_s", FRESH)
    _run_entry(context, "main.run", "--target aws")
    assert context.exit_code == 0, context.printed + str(context.error)
    context.calls.clear()
    context.printed, context.sleeps = "", []


@given('everything a run and its three extensions create exists for PREFIX "{prefix}"')
def step_everything(context, prefix):
    os.environ["PREFIX"] = prefix
    context.calls.select_returns("FROM trips_current", CHANGES)
    context.calls.select_returns("max_change_to_arrival_s", FRESH)
    context.calls.select_returns("WHERE trip_id = 'probe-", _probe_row)
    for name, args in (("main.run", "--target aws"), ("lag.py", "--target aws"),
                       ("maintenance.py", "--target aws --schedule")):
        _run_entry(context, name, args)
        assert context.exit_code == 0, (name, context.printed, context.error)
    # Also the two nightly schedules of ext-maintenance before its revision (Athena started directly, in the
    # default group, with the role $PREFIX-cdc-scheduler-role): an account deployed then still has them.
    _legacy_schedules()
    # What the stand-ins do not make: the Glue table and view (Athena's DDL), and the objects Firehose and
    # Athena write.
    from main.config import Settings

    s = Settings()
    context.bucket = s.bucket("123456789012")
    for table in (s.lake_table, s.view):
        support.client("glue").create_table(DatabaseName=s.database, TableInput={"Name": table})
    for key in ("lake/trip_changes/data/x.parquet", "athena-results/q.csv", "firehose-errors/e"):
        support.client("s3").put_object(Bucket=context.bucket, Key=key, Body=b"x")
    context.calls.clear()
    context.printed, context.sleeps, context.runs = "", [], []


def _legacy_schedules():
    import boto3

    from main.config import Settings
    from main.infra import ensure_roles

    s = ensure_roles(boto3.client("iam", region_name="ca-central-1"), Settings(), "123456789012", "",
                     which=("scheduler",))
    sch = support.client("scheduler")
    for name, cron, sql in ((f"{s.prefix}-lake-optimize", "cron(0 3 * * ? *)",
                             f"OPTIMIZE {s.database}.{s.lake_table} REWRITE DATA USING BIN_PACK"),
                            (f"{s.prefix}-lake-vacuum", "cron(0 4 * * ? *)", f"VACUUM {s.database}.{s.lake_table}")):
        sch.create_schedule(Name=name, ScheduleExpression=cron, ScheduleExpressionTimezone="America/Vancouver",
                            FlexibleTimeWindow={"Mode": "OFF"}, State="ENABLED",
                            Target={"Arn": "arn:aws:scheduler:::aws-sdk:athena:startQueryExecution",
                                    "RoleArn": s.scheduler_role_arn,
                                    "Input": json.dumps({"QueryString": sql, "WorkGroup": s.workgroup})})


@then("the account has no schedule, alarm, mapping, function, log group, queue, Firehose stream, Glue database, bucket, table or role left")
def step_empty(context):
    found = support.inventory()
    # What stays by design: the shared lake bucket of simulated-runs (scripts/lake.py down deletes it), and the
    # Glue database that holds trip_events_parquet, which ext-maintenance keeps for the landing path.
    shared = {"buckets": ["recipe-ana-lake-123456789012"], "glue": ["recipe_ana_lake"]}
    left = {k: [x for x in v if x not in shared.get(k, [])] if isinstance(v, list) else v for k, v in found.items()}
    assert not any(left.values()), found


@given("the account is noted")
def step_note(context):
    context.noted = support.inventory()
    context.calls.clear()


@then("the account is as it was noted")
def step_same(context):
    assert support.inventory() == context.noted


@then('no call whose name starts with "{start}" was made')
def step_no_calls_starting(context, start):
    assert not [op for op in context.calls.names() if op.startswith(start)], context.calls.names()


@then('the second run printed no line that starts with "{start}"')
def step_second_run(context, start):
    second = context.runs[-1][len(context.runs[-2]):]
    assert second.strip() and not [l for l in second.splitlines() if l.startswith(start)], second


@given('the alarm "{alarm}" and the role "{role}" of a teammate exist')
def step_teammate(context, alarm, role):
    support.client("cloudwatch").put_metric_alarm(
        AlarmName=alarm, MetricName="m", Namespace="n", Statistic="Maximum", Period=60, EvaluationPeriods=1,
        Threshold=1.0, ComparisonOperator="GreaterThanThreshold")
    support.client("iam").create_role(RoleName=role, AssumeRolePolicyDocument="{}")
    context.calls.clear()


@given('the role "{role}" does not carry the recipe\'s tag')
def step_untag(context, role):
    support.client("iam").untag_role(RoleName=role, TagKeys=["recipe"])
    context.calls.clear()


@then('the only alarm left is "{alarm}"')
def step_alarm_left(context, alarm):
    assert support.inventory()["alarms"] == [alarm]


@then('the roles left are "{roles}"')
def step_roles_left(context, roles):
    assert support.inventory()["roles"] == sorted(_lst(roles)), support.inventory()["roles"]


@given('the recipe\'s objects are in the bucket "{bucket}", passed in as BUCKET, beside the object "{other}"')
def step_bucket_passed(context, bucket, other):
    s3 = support.client("s3")
    s3.create_bucket(Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": "ca-central-1"})
    for key in ("lake/trip_changes/data/x.parquet", "athena-results/q.csv", "firehose-errors/e", "freshness/f.json", other):
        s3.put_object(Bucket=bucket, Key=key, Body=b"x")
    os.environ["BUCKET"] = bucket
    context.calls.clear()


@then('the bucket "{bucket}" is still there, with only the object "{key}"')
def step_bucket_left(context, bucket, key):
    assert [o for o in support.inventory()["objects"] if o.startswith(bucket + "/")] == [f"{bucket}/{key}"]


@then('the bucket "{bucket}" is still there')
def step_bucket_there(context, bucket):
    assert bucket in support.inventory()["buckets"]


@given("the table, the Firehose stream and the event source mapping are being deleted")
def step_being_deleted(context):
    context.calls.change("ListEventSourceMappings", lambda r: {**r, "EventSourceMappings": [
        {**m, "State": "Deleting"} for m in r["EventSourceMappings"]]})
    context.calls.answer("DescribeDeliveryStream", {"DeliveryStreamDescription": {"DeliveryStreamStatus": "DELETING"}})
    context.calls.answer("DescribeTable", {"Table": {"TableStatus": "DELETING"}})


@then("no call was made to delete the table, the Firehose stream or the mapping again")
def step_not_again(context):
    made = set(zip(context.calls.services, context.calls.names()))
    again = {("firehose", "DeleteDeliveryStream"), ("lambda", "DeleteEventSourceMapping")}
    assert not again & made, again & made
    # the trips table only: ext-scheduled-merge's clean-up may delete the run log $PREFIX-runs, which is not deleting
    assert "recipe-ana-trips" not in [p.get("TableName") for p in context.calls.of("DeleteTable")]
    assert context.printed.count("(already being deleted)") == 3, context.printed


@given('S3 answers that it could not delete {n:d} object under "{prefix}"')
def step_s3_errors(context, n, prefix):
    context.calls.answer("DeleteObjects", lambda p: {"Errors": [
        {"Key": o["Key"], "Code": "AccessDenied", "Message": "Access Denied"} for o in p["Delete"]["Objects"][:n]]},
        when=lambda p: p["Delete"]["Objects"][0]["Key"].startswith(prefix))


# ---- ext-lag-alarm

@given("Athena returns the probe's row, changed {seconds:d} seconds ago, at its {n:d}th look")
def step_probe_answer(context, seconds, n):
    context.calls.select_returns("WHERE trip_id = 'probe-", _probe_row, after_tries=n)
    context.calls.select_returns("max_change_to_arrival_s", FRESH)


@then('a probe trip was written to "{table}" and removed again')
def step_probe(context, table):
    (put,) = [p for p in context.calls.of("PutItem") if p["TableName"] == table]
    (deleted,) = [p for p in context.calls.of("DeleteItem") if p["TableName"] == table]
    assert put["Item"]["trip_id"].startswith("probe-") and deleted["Key"]["trip_id"] == put["Item"]["trip_id"]


@then('the metric "{metric}" got one value between {low:d} and {high:d} seconds')
def step_metric(context, metric, low, high):
    (put,) = context.calls.of("PutMetricData")
    (datum,) = put["MetricData"]
    assert f"{put['Namespace']}/{datum['MetricName']}" == metric and low <= datum["Value"] <= high, datum
    assert datum["Unit"] == "Seconds"


@then('the object "{key}" in the lake\'s bucket names the table, "{field}" and the lag')
def step_freshness(context, key, field):
    (put,) = [p for p in context.calls.of("PutObject") if p["Key"] == key]
    doc = json.loads(put["Body"])
    assert doc["table"] == "trip_changes" and doc[field] == "2026-11-13T04:28:56+00:00", doc
    assert 90 <= doc["change_to_queryable_s"] <= 93 and put["Bucket"] == "recipe-ana-cdc-lake-123456789012"


def _alarm(context, name: str) -> dict:
    alarms = [a for a in context.calls.of("PutMetricAlarm") if a["AlarmName"] == name]
    assert alarms, context.calls.names()
    return alarms[-1]


@then('the alarm "{name}" goes off when "{metric}" is greater than {limit:d} in one period of {period:d} seconds')
def step_alarm_lag(context, name, metric, limit, period):
    a = _alarm(context, name)
    assert (a["MetricName"], a["Threshold"], a["ComparisonOperator"], a["Period"], a["EvaluationPeriods"],
            a["DatapointsToAlarm"], a["Statistic"]) == (metric, float(limit), "GreaterThanThreshold", period, 1, 1,
                                                        "Maximum"), a


@then('the printed probe "{label}" is {side} the limit with the alarm "{state}"')
def step_printed_probe(context, label, side, state):
    (line,) = [l for l in context.printed.splitlines() if l.startswith(label)]
    m = re.search(r"change to queryable\s+(\d+) s\s+limit (\d+) s\s+alarm (\w+)", line)
    lag, limit = int(m.group(1)), int(m.group(2))
    assert m.group(3) == state and (lag < limit if side == "under" else lag > limit), line


@then('the alarm "{name}" goes off when "{metric}" of the function "{function}" is greater than {limit:d} in {m:d} of {n:d} periods of {period:d} seconds')
def step_alarm_age(context, name, metric, function, limit, m, n, period):
    a = _alarm(context, name)
    assert (a["Namespace"], a["MetricName"], a["Threshold"], a["DatapointsToAlarm"], a["EvaluationPeriods"],
            a["Period"]) == ("AWS/Lambda", metric, float(limit), m, n, period), a
    assert a["Dimensions"] == [{"Name": "FunctionName", "Value": function}] and a["TreatMissingData"] == "notBreaching"


@then('the alarm "{name}" treats missing data as "{how}"')
def step_missing(context, name, how):
    assert _alarm(context, name)["TreatMissingData"] == how


@then('the recipe\'s own reading of an alarm without a value is "{state}"')
def step_no_value(context, state):
    import lag
    from main.config import Settings

    assert lag.alarm_state(lag.alarm_definitions(Settings())[0], []) == state


# ---- ext-rollback-or-schema

def _line(context, start: str) -> str:
    (line,) = [l for l in context.printed.splitlines() if l.startswith(start)]
    return line


def _changes_and_fare(line: str) -> tuple[int, float]:
    m = re.search(r"(\d+) changes, average fare ([\d.]+)", line)
    return int(m.group(1)), float(m.group(2))


@then("before the load the table has {n:d} changes with an average fare under {limit:d}")
def step_before_load(context, n, limit):
    changes, fare = _changes_and_fare(_line(context, "before the load"))
    assert changes == n and fare < limit, (changes, fare)
    context.fare_before = fare


@then("after the load the average fare is more than {times:d} times that")
def step_after_load(context, times):
    _, fare = _changes_and_fare(_line(context, "after the load"))
    assert fare > times * context.fare_before, fare


@then("time travel to the snapshot before the load still finds {n:d} changes")
def step_time_travel(context, n):
    before = re.search(r"snapshot (\d+)", _line(context, "before the load")).group(1)
    assert _line(context, "time travel to").startswith(f"time travel to {before}: {n} changes"), context.printed


@then("after the restore the table has {total:d} changes, the {old:d} and {good:d} good change made after the load, with an average fare under {limit:d}")
def step_after_restore(context, total, old, good, limit):
    changes, fare = _changes_and_fare(_line(context, "after restore"))
    assert changes == total == old + good and fare < limit, (changes, fare)
    assert _line(context, "rolled back to").endswith(f"good rows from later commits appended again: {good}")


@then('the columns end with "{column}"')
def step_columns(context, column):
    assert _line(context, "columns now:").endswith(f", {column}")


@then('every change before the new column reads "{old}" as its payment_type, and the new one "{new}"')
def step_payment_types(context, old, new):
    values = [v.split("=")[1] for v in _line(context, "payment_type per change:").split(": ", 1)[1].split(", ")]
    assert values[-1] == new and len(values) > 1 and set(values[:-1]) == {old}, values


@then('Athena ran "{statement}"')
def step_athena_ran(context, statement):
    assert context.calls.queries == [statement], context.calls.queries


@then("the output names the {n:d} steps that fill the column")
def step_three_steps(context, n):
    assert len(re.findall(r"^  \d\. ", context.printed, flags=re.M)) == n, context.printed


# ---- ext-maintenance

def _measure(context, label: str) -> list[float]:
    return [float(x) for x in re.findall(r"[\d.]+", _line(context, label))]


@then('the printed row "after" has fewer data files, fewer snapshots and less data scanned than the row "before"')
def step_fewer(context):
    before, after = _measure(context, "before"), _measure(context, "after")
    assert after[0] < before[0] and after[1] < before[1] and after[2] < before[2], (before, after)


@then('Athena ran, in this order, the measured query, "{optimize}", "{vacuum}" and the measured query again')
def step_maintenance_order(context, optimize, vacuum):
    q = context.calls.queries
    assert len(q) == 4 and q[1] == optimize and q[2] == vacuum and q[0] == q[3] and q[0].startswith("SELECT "), q


@then('the output has a row "before" and a row "after" with Data scanned in B and run time in ms')
def step_before_after(context):
    for label in ("before", "after"):
        assert re.fullmatch(rf"{label}\s+\d+ B\s+\d+ ms", _line(context, label)), _line(context, label)


@then("no schedule was created")
def step_no_schedule(context):
    assert "CreateSchedule" not in context.calls.names()


@then('the schedules "{a}" at "{cron_a}" and "{b}" at "{cron_b}" exist, in the time zone "{zone}"')
def step_schedules(context, a, cron_a, b, cron_b, zone):
    made = {s["Name"]: s for s in context.calls.of("CreateSchedule")}
    assert sorted(made) == sorted([a, b]), sorted(made)
    assert made[a]["ScheduleExpression"] == cron_a and made[b]["ScheduleExpression"] == cron_b
    assert all(s["ScheduleExpressionTimezone"] == zone and s["State"] == "ENABLED" for s in made.values())
    assert support.inventory()["schedules"] == sorted([a, b])


@then('each schedule starts an Athena query as the role "{role}"')
def step_schedule_target(context, role):
    for s in context.calls.of("CreateSchedule"):
        target = s["Target"]
        assert target["Arn"] == "arn:aws:scheduler:::aws-sdk:athena:startQueryExecution", target
        assert target["RoleArn"].endswith(f":role/{role}") and "QueryString" in json.loads(target["Input"])

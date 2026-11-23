"""Steps for the five features. Each When starts a script of the recipe as the command line does (its main(),
with the options of the step), or sends a request to the function URL the recipe deployed."""

from __future__ import annotations

import datetime as dt
import io
import json
import os
import re
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from urllib.parse import urlencode

import support
from behave import given, then, when
from support import ACCOUNT, ROOT

WINDOW = {"start": "2026-11-12T15:00:00.000Z", "end": "2026-11-12T16:00:00.000Z"}   # of --as-of 2026-11-12T16:00:30Z


def _lst(text: str) -> list[str]:
    return [t.strip() for t in text.split(",") if t.strip()]


def _lines(text: str) -> list[str]:
    return text.splitlines()


# ---- settings and stand-in rules

@given('the setting {name} is "{value}"')
def step_env(context, name, value):
    if value == "not set":
        os.environ.pop(name, None)
    else:
        os.environ[name] = value


@given("no AWS credentials, no Region and no gateway settings in the environment")
def step_no_credentials(context):
    for name in [n for n in os.environ if n.startswith("AWS_") and n != "AWS_EC2_METADATA_DISABLED"]:
        os.environ.pop(name)


@given('AWS refuses every "{operation}" call with "{code}"')
def step_refuse(context, operation, code):
    context.calls.refuse(operation, code)


@given("Firehose has written {n:d} Parquet files")
def step_firehose_files(context, n):
    files = [{"Key": f"updates/dt=2026-11-12/recipe-to-lake-{i}.parquet", "Size": 6000} for i in range(n)]
    context.calls.answer("ListObjectsV2", {"Contents": files, "KeyCount": n},
                         when=lambda p: p.get("Prefix") == "updates/")


@given("Athena answers the count with {n:d}, after scanning {scanned:d} bytes")
def step_athena(context, n, scanned):
    context.calls.answer("GetQueryResults", {"ResultSet": {"Rows": [
        {"Data": [{"VarCharValue": "updates_last_hour"}]}, {"Data": [{"VarCharValue": str(n)}]}]}})

    def with_scanned(answer, params):
        answer["QueryExecution"].setdefault("Statistics", {})["DataScannedInBytes"] = scanned
        return answer
    context.calls.change("GetQueryExecution", with_scanned)


# ---- files of updates

def _write(context, name: str, rows: list[dict]) -> None:
    context.file = context.tmp / name
    context.file.write_text("".join(json.dumps(r) + "\n" for r in rows))


@given("a file of updates with the event times")
def step_file(context):
    _write(context, "updates.jsonl", [{"event_id": r["event_id"], "entity_id": "st-01", "event_time": r["event_time"],
                                       "value": 1} for r in context.table])


@given("a file of updates where one has no entity_id")
def step_file_lacks(context):
    _write(context, "updates.jsonl", [
        {"event_id": "a", "entity_id": "st-01", "event_time": "2026-11-12T15:10:00.000Z", "value": 1},
        {"event_id": "b", "event_time": "2026-11-12T15:20:00.000Z", "value": 2}])


@given('a stream-sim file of 3 updates in the window and 1 before it, with the payload field "{field}"')
def step_streamsim(context, field):
    times = ["2026-11-12T14:00:00.000Z", "2026-11-12T15:10:00.000Z", "2026-11-12T15:20:00.000Z",
             "2026-11-12T15:30:00.000Z"]
    _write(context, "stream.jsonl", [{"event_id": f"e-{i}", "partition_key": f"st-0{i % 2 + 1}", "event_time": t,
                                      "data": {field: 5 + i}} for i, t in enumerate(times)])


# ---- running the scripts

def _run(context, script: str, argv: list[str]) -> None:
    if script.startswith("rep-") and "aws" not in argv and context.runs:
        # An offline run starts its own moto and expects it empty; inside the scenario's moto a second offline run
        # would find the first one's table. Start from an empty account, as two runs on a laptop do.
        context.moto.stop()
        context.moto.start()
    context.marks = getattr(context, "marks", []) + [len(context.calls.made)]
    support.run_script(context, script, argv)
    context.runs.append(context.last)


@when('{script} runs with "{args}"')
def step_run(context, script, args):
    _run(context, script, args.split())


@when('{script} runs with that file and "{args}"')
def step_run_file(context, script, args):
    _run(context, script, ["--events", str(context.file), *args.split()])


@when('{script} runs with that stream-sim file and "{args}"')
def step_run_streamsim(context, script, args):
    _run(context, script, ["--streamsim", str(context.file), *args.split()])


@when("clean_up.py runs")
def step_clean_up(context):
    # the clean-up contract: a bare run only lists; a run that deletes passes --yes
    _run(context, "clean_up.py", ["--yes"])


@then("the exit code is {code:d}")
def step_code(context, code):
    assert context.exit_code == code and (context.error is None or code != 0), (context.exit_code, context.error,
                                                                               context.printed[-1500:])


@then('the output holds "{text}"')
def step_output(context, text):
    assert text in context.printed, context.printed[-2500:]


@then('the run stops with the error "{text}"')
def step_error(context, text):
    assert context.exit_code == 1 and text in repr(context.error) + context.printed, (context.error,
                                                                                      context.printed[-800:])


@then('no printed line starts with "{start}"')
def step_no_line(context, start):
    assert not [ln for ln in _lines(context.printed) if ln.startswith(start)], context.printed


@then('at least {n:d} printed line starts with "{start}"')
def step_some_line(context, n, start):
    assert len([ln for ln in _lines(context.printed) if ln.startswith(start)]) >= n, context.printed


@then('{n:d} printed lines hold "{text}"')
def step_n_lines(context, n, text):
    found = [ln for ln in _lines(context.printed) if text in ln]
    assert len(found) == n, found


@then('the second run printed no line that starts with "{a}" or "{b}"')
def step_second(context, a, b):
    assert len(context.runs) == 2
    assert not [ln for ln in _lines(context.runs[1]) if ln.startswith((a, b))], context.runs[1]


# ---- the number

def _numbers(printed: str) -> list[int]:
    found = re.findall(r"updates in the last hour: (\d+) |API (\d+)|lake (\d+)|table (\d+)|tool result (\d+)|"
                       r"the API's number is (\d+)", printed)
    return [int(n) for groups in found for n in groups if n]


@then("the four runs print the same number of updates in the last hour, the one plain Python counts")
def step_same_number(context):
    from skeleton.events import distinct_in_window, generate, parse
    from skeleton.number import window

    as_of = parse("2026-11-12T16:00:30Z")
    expected = distinct_in_window(generate(300, now=as_of), *window(as_of))
    found = _numbers(context.printed)
    assert len(context.runs) == 4 and len(found) == 7 and set(found) == {expected}, (found, expected)


@then('the page ".local/{page}" shows the number the run printed')
def step_page(context, page):
    number = _numbers(context.printed)[0]
    assert f"<b>{number}</b>" in (context.tmp / ".local" / page).read_text()


@then("no page for users was written")
def step_no_page(context):
    assert not list((context.tmp / ".local").glob("*.html")), list((context.tmp / ".local").glob("*"))


@then("no table was created")
def step_no_table(context):
    assert not context.calls.of("CreateTable"), context.calls.names()


@then("no gateway client was made")
@then("no model was called")
def step_no_gateway(context):
    assert not support.GATEWAYS


# ---- names, Region

def _nothing() -> bool:
    return not any(support.inventory().values())


@then("the account has nothing of the recipe")
def step_nothing(context):
    assert _nothing(), support.inventory()


@then("every stream, Firehose stream, table, function, log group, bucket, workgroup and role of the account "
      'starts with "{start}"')
def step_prefix_names(context, start):
    found = support.inventory()
    for kind in ("streams", "firehose", "tables", "functions", "buckets", "workgroups", "roles"):
        assert found[kind] and all(n.startswith(start) for n in found[kind]), (kind, found[kind])
    assert found["log groups"] == [f"/aws/lambda/{start}api"], found["log groups"]


@then('the bucket is "{bucket}" and the Glue database "{database}"')
def step_bucket_database(context, bucket, database):
    found = support.inventory()
    assert (found["buckets"], found["glue"]) == ([bucket], [database]), found


@then('every call went to the Region "{region}"')
def step_regions(context, region):
    found = {r for r, s in zip(context.calls.regions, context.calls.services) if s != "iam"}
    assert found == {region}, found
    assert {"kinesis", "firehose", "dynamodb", "lambda", "athena", "glue", "s3"} <= set(context.calls.services)


@then('the bucket "{bucket}" has S3 delete objects under "{prefix}" after {days:d} days')
def step_lifecycle(context, bucket, prefix, days):
    context.rules = support.client("s3").get_bucket_lifecycle_configuration(Bucket=bucket)["Rules"]
    found = [(r["Status"], r["Filter"], r["Expiration"]) for r in context.rules]
    assert ("Enabled", {"Prefix": prefix}, {"Days": days}) in found, context.rules


@then("the bucket has no other lifecycle rule")
def step_one_rule(context):
    assert len(context.rules) == 1, context.rules


@given('S3 answers "{code}" when the bucket is created')
def step_bucket_taken(context, code):
    context.calls.refuse("CreateBucket", code)


@then("no stream and no Firehose stream was created")
def step_no_stream(context):
    assert not context.calls.of("CreateStream") and not context.calls.of("CreateDeliveryStream")


# ---- lakehouse first

@then('the stream "{stream}" was created with {n:d} shard, in provisioned mode')
def step_stream(context, stream, n):
    (made,) = context.calls.of("CreateStream")
    assert made == {"StreamName": stream, "ShardCount": n, "StreamModeDetails": {"StreamMode": "PROVISIONED"}}, made


def _firehose(context) -> dict:
    (made,) = context.calls.of("CreateDeliveryStream")
    return made


@then('the Firehose stream "{name}" reads that stream and writes to the bucket "{bucket}" under "{prefix}"')
def step_firehose(context, name, bucket, prefix):
    made = _firehose(context)
    stream = context.calls.of("CreateStream")[0]["StreamName"]
    dest = made["ExtendedS3DestinationConfiguration"]
    assert made["DeliveryStreamName"] == name and made["DeliveryStreamType"] == "KinesisStreamAsSource"
    assert made["KinesisStreamSourceConfiguration"]["KinesisStreamARN"].endswith(f":stream/{stream}")
    assert (dest["BucketARN"], dest["Prefix"]) == (f"arn:aws:s3:::{bucket}", prefix), dest


@then('it writes a file after {seconds:d} seconds or {mb:d} MB, as Parquet with "{compression}", with the schema '
      'of the Glue table "{table}"')
def step_firehose_format(context, seconds, mb, compression, table):
    dest = _firehose(context)["ExtendedS3DestinationConfiguration"]
    conv = dest["DataFormatConversionConfiguration"]
    assert dest["BufferingHints"] == {"SizeInMBs": mb, "IntervalInSeconds": seconds}, dest["BufferingHints"]
    assert conv["Enabled"] is True
    assert conv["OutputFormatConfiguration"] == {"Serializer": {"ParquetSerDe": {"Compression": compression}}}
    assert conv["SchemaConfiguration"]["TableName"] == table


@then('the Glue table "{table}" of "{database}" has the columns "{columns}"')
def step_glue(context, table, database, columns):
    (made,) = context.calls.of("CreateTable", "glue")
    context.glue_table = made["TableInput"]
    assert made["DatabaseName"] == database and context.glue_table["Name"] == table
    assert [c["Name"] for c in context.glue_table["StorageDescriptor"]["Columns"]] == _lst(columns)


@then('its partition "{name}" is worked out by partition projection from "{template}"')
def step_projection(context, name, template):
    t = context.glue_table
    assert [p["Name"] for p in t["PartitionKeys"]] == [name]
    assert t["Parameters"]["projection.enabled"] == "true" and t["Parameters"][f"projection.{name}.type"] == "date"
    assert t["Parameters"]["storage.location.template"] == template, t["Parameters"]


@then('the workgroup "{name}" cancels a query that would scan more than {n:d} bytes')
def step_workgroup(context, name, n):
    (made,) = context.calls.of("CreateWorkGroup")
    context.workgroup = made
    assert made["Name"] == name and made["Configuration"]["BytesScannedCutoffPerQuery"] == n, made


@then('it writes results under "{location}" whatever the query asks for')
def step_workgroup_results(context, location):
    config = context.workgroup["Configuration"]
    assert config["ResultConfiguration"]["OutputLocation"] == location and config["EnforceWorkGroupConfiguration"]


@then('every query went to the workgroup "{name}"')
def step_queries_workgroup(context, name):
    queries = context.calls.of("StartQueryExecution")
    assert queries and all(q["WorkGroup"] == name for q in queries), queries


def _flat(value) -> list[str]:
    return value if isinstance(value, list) else [value]


@then('the role "{role}" may read the stream "{stream}", write the bucket "{bucket}" and read the Glue table '
      '"{table}"')
def step_firehose_role(context, role, stream, bucket, table):
    context.role = role
    database = table.split("/")[0]
    glue = f"arn:aws:glue:ca-central-1:{ACCOUNT}"
    found = {(tuple(sorted(a.split(":")[0] for a in s["Action"]))[0], r) for s in support.policy(role)
             for r in _flat(s["Resource"])}
    assert found == {("kinesis", f"arn:aws:kinesis:ca-central-1:{ACCOUNT}:stream/{stream}"),
                     ("s3", f"arn:aws:s3:::{bucket}"), ("s3", f"arn:aws:s3:::{bucket}/*"),
                     ("glue", f"{glue}:catalog"), ("glue", f"{glue}:database/{database}"),
                     ("glue", f"{glue}:table/{table}")}, found
    actions = {a for s in support.policy(role) for a in s["Action"]}
    assert not [a for a in actions if a.split(":")[1].startswith(("Delete", "Create", "Update"))], actions
    assert "s3:PutObject" in actions and "kinesis:GetRecords" in actions and "glue:GetTableVersions" in actions


@then('no statement of its policy has "*" as a resource or in an action')
def step_no_star(context):
    for s in support.policy(context.role):
        assert "*" not in _flat(s["Resource"]) and not [a for a in s["Action"] if "*" in a], s
        assert s["Effect"] == "Allow"


@then('the role can be assumed by "{service}" only')
def step_trust(context, service):
    doc = support.client("iam").get_role(RoleName=context.role)["Role"]["AssumeRolePolicyDocument"]
    doc = json.loads(doc) if isinstance(doc, str) else doc
    assert [s["Principal"] for s in doc["Statement"]] == [{"Service": service}], doc


@given('{variable} names the role "{role}" of the account')
def step_role_passed(context, variable, role):
    trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "sts:AssumeRole", "Principal": {
        "Service": ["lambda.amazonaws.com", "firehose.amazonaws.com"]}}]}
    os.environ[variable] = support.client("iam").create_role(
        RoleName=role, AssumeRolePolicyDocument=json.dumps(trust))["Role"]["Arn"]
    context.calls.clear_made()


@then("no role was created")
def step_no_role(context):
    assert not context.calls.of("CreateRole") and not context.calls.of("PutRolePolicy"), context.calls.names()


@then('the Firehose stream acts as "{arn}"')
def step_firehose_as(context, arn):
    made = _firehose(context)
    assert made["KinesisStreamSourceConfiguration"]["RoleARN"] == arn
    assert made["ExtendedS3DestinationConfiguration"]["RoleARN"] == arn


def _refuse_first(n: int, calls_left: list):
    def change(answer, params):
        if calls_left[0] == 0:
            return answer
        calls_left[0] -= 1
        for record in answer["Records"][:n]:
            record.clear()
            record.update(ErrorCode="ProvisionedThroughputExceededException", ErrorMessage="Rate exceeded")
        answer["FailedRecordCount"] = min(n, len(answer["Records"]))
        return answer
    return change


@given("the stream refuses the first {n:d} records of its first {calls:d} calls")
def step_refuse_once(context, n, calls):
    context.calls.change("PutRecords", _refuse_first(n, [calls]))


@given("the stream refuses the first {n:d} records of every call")
def step_refuse_always(context, n):
    context.calls.change("PutRecords", _refuse_first(n, [-1]))


@then('the PutRecords calls carry "{counts}" records')
def step_put_counts(context, counts):
    found = [len(p["Records"]) for p in context.calls.of("PutRecords")]
    assert found == [int(c) for c in _lst(counts)], found


@then("every record has its entity_id as partition key")
def step_partition_key(context):
    records = [r for p in context.calls.of("PutRecords") for r in p["Records"]]
    assert records and all(r["PartitionKey"] == json.loads(r["Data"])["entity_id"] for r in records)


@then("no record was put on the stream")
def step_no_records(context):
    assert not context.calls.of("PutRecords")


@then('the count Athena was asked for is of distinct event_id, from the folder "dt={day}" on, with event times '
      'from "{start}" up to "{end}"')
def step_lake_query(context, day, start, end):
    (sql,) = [q["QueryString"] for q in context.calls.of("StartQueryExecution")]
    assert "count(DISTINCT event_id)" in sql and f"dt >= '{day}'" in sql, sql
    assert f"event_time >= TIMESTAMP '{start}'" in sql and f"event_time < TIMESTAMP '{end}'" in sql, sql


@then('every Parquet file is under ".local/lakehouse/updates/dt=<today>/"')
def step_parquet_folder(context):
    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    files = list((context.tmp / ".local" / "lakehouse").rglob("*.parquet"))
    assert files and all(f.parent.name in (f"dt={today}",) for f in files), files


@given('the stream "{stream}" is in the DELETING state until the recipe has waited for it')
def step_stream_deleting(context, stream):
    context.calls.refuse("CreateStream", "ResourceInUseException", times=1)
    context.calls.answer("DescribeStreamSummary",
                         {"StreamDescriptionSummary": {"StreamName": stream, "StreamStatus": "DELETING"}}, times=2)
    context.calls.refuse("DescribeStreamSummary", "ResourceNotFoundException", times=1)


@then("the recipe asked for the stream {n:d} times, and looked whether it was gone in between")
def step_stream_twice(context, n):
    names = context.calls.names()
    creates = [i for i, op in enumerate(names) if op == "CreateStream"]
    assert len(creates) == n and names[creates[0]:creates[1]].count("DescribeStreamSummary") == 3, names[:14]
    assert 5 in context.sleeps


@given("the Firehose stream is in the DELETING state until the recipe has waited for it")
def step_firehose_deleting(context):
    context.calls.answer("DescribeDeliveryStream",
                         {"DeliveryStreamDescription": {"DeliveryStreamStatus": "DELETING"}}, times=2)
    context.calls.refuse("DescribeDeliveryStream", "ResourceNotFoundException", times=1)


@then("the Firehose stream was created after it was gone")
def step_firehose_after(context):
    names = context.calls.names()
    assert names[:names.index("CreateDeliveryStream")].count("DescribeDeliveryStream") == 3, names
    assert 5 in context.sleeps


@given('the Firehose stream\'s status is "{status}"')
def step_firehose_status(context, status):
    context.calls.answer("DescribeDeliveryStream", {"DeliveryStreamDescription": {"DeliveryStreamStatus": status}})


# ---- operational first: the API

def _send(context, method: str, path: str, data: bytes | None = None, params: dict | None = None, signed=True):
    """One request to the function URL, signed as the recipe signs (Signature Version 4, service lambda)."""
    import boto3
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest

    full = context.url.rstrip("/") + path + (("?" + urlencode(params)) if params else "")
    headers = {"content-type": "application/json"}
    if signed:
        req = AWSRequest(method=method, url=full, data=data, headers=headers)
        SigV4Auth(boto3.Session().get_credentials().get_frozen_credentials(), "lambda", "ca-central-1").add_auth(req)
        headers = dict(req.headers)
    try:
        with support.no_waits(context):        # the function's own waits between reads are written down, not waited
            with urllib.request.urlopen(urllib.request.Request(full, data=data, method=method, headers=headers),
                                        timeout=30) as resp:
                context.status, context.body = 200, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        context.status, context.body = e.code, json.loads(e.read())


def _post(context, updates) -> None:
    _send(context, "POST", "/updates", json.dumps({"updates": updates}).encode())


@given('the API of PREFIX "{prefix}" is deployed')
def step_api(context, prefix):
    import boto3
    import operational
    from skeleton.config import names

    os.environ["PREFIX"] = prefix
    context.names = names(prefix, account=ACCOUNT)
    with support.no_waits(context):
        context.url = operational.setup(boto3.Session(region_name="ca-central-1"), context.names, ACCOUNT)
    context.calls.clear_made()
    context.sleeps.clear()


def _rows(table) -> list[dict]:
    return [{**dict(zip(r.headings, r.cells)), "value": int(r["value"])} for r in table]


@when("a signed POST /updates carries")
def step_post(context):
    context.sent = _rows(context.table)
    _post(context, context.sent)


@when("a signed POST /updates carries the body {body}")
def step_post_body(context, body):
    if body == "101 updates":
        body = json.dumps({"updates": [{"event_id": f"e{i}", "entity_id": "s", "value": 1,
                                        "event_time": "2026-11-12T15:04:05.123Z"} for i in range(101)]})
    if body == "an entity_id of 65 characters":
        body = json.dumps({"updates": [{"event_id": "a", "entity_id": "s" * 65, "value": 1,
                                        "event_time": "2026-11-12T15:04:05.123Z"}]})
    _send(context, "POST", "/updates", body.encode())


@when("a signed POST /updates carries one good update and then one without an event_id")
def step_post_one_bad(context):
    _post(context, [{"event_id": "a", "entity_id": "s", "event_time": "2026-11-12T15:04:05.123Z", "value": 1},
                    {"entity_id": "s", "event_time": "2026-11-12T15:04:06.123Z", "value": 2}])


@given('updates were stored at "{times}"')
def step_stored_at(context, times):
    _post(context, [{"event_id": f"ev-{i}", "entity_id": "st-01", "event_time": t, "value": i}
                    for i, t in enumerate(_lst(times))])
    assert context.status == 200, context.body
    context.calls.clear_made()


def _unread(times: int):
    def change(answer, params):
        return {"Responses": {}, "UnprocessedKeys": params["RequestItems"]}
    return change, times


@given("DynamoDB leaves the counters of every read unread")
def step_unread_always(context):
    context.calls.change("BatchGetItem", _unread(-1)[0])


@given("after the table was built, DynamoDB leaves the counters of every read unread")
def step_unread_after_table(context):
    """The offline build reads the counters too (its GET /number) and ends with a Scan: only reads after it."""
    built = lambda params: any(op == "Scan" for op, _ in context.calls.made)
    context.calls.change("BatchGetItem", _unread(-1)[0], when=built)


@given("DynamoDB leaves the counters of the first read unread")
def step_unread_once(context):
    context.calls.change("BatchGetItem", _unread(1)[0], times=1)


@then("the function asked for the counters {n:d} times")
def step_reads(context, n):
    assert len(context.calls.of("BatchGetItem")) == n, context.calls.names()
    assert len(context.sleeps) == n - 1 and context.sleeps == sorted(context.sleeps), context.sleeps   # growing waits


@then("after the table was built, the tool asked DynamoDB for the counters {n:d} times")
def step_tool_read_count(context, n):
    assert [op for op, _ in _after_table(context)] == ["BatchGetItem"] * n, _after_table(context)


@when('a signed GET /number asks as of "{as_of}"')
def step_get_number(context, as_of):
    _send(context, "GET", "/number", params={"as_of": as_of})


@when('a signed GET /current asks for "{entity}"')
def step_get_current(context, entity):
    _send(context, "GET", "/current", params={"entity_id": entity})


@when('a signed GET {path} asks with "{query}"')
def step_get_any(context, path, query):
    _send(context, "GET", path, params=dict(pair.split("=") for pair in query.split("&")))


@then("the function wrote one log line: {line}")
def step_log_line(context, line):
    written = support.URL_REQUESTS[-1]["log"].splitlines()
    assert len(written) == 1 and json.loads(written[0]) == json.loads(line), written


@then('the log line holds none of "{texts}"')
def step_log_no_ids(context, texts):
    assert not [t for t in _lst(texts) if t in support.URL_REQUESTS[-1]["log"]], support.URL_REQUESTS[-1]["log"]


@when("an unsigned GET /number is sent")
def step_unsigned(context):
    _send(context, "GET", "/number", signed=False)


@then("the API answers {status:d} with stored {stored:d}, duplicates {duplicates:d} and older {older:d}")
def step_api_stored(context, status, stored, duplicates, older):
    assert (context.status, context.body) == (status, {"stored": stored, "duplicates": duplicates, "older": older}), (
        context.status, context.body)


@then('the API answers {status:d} with an error that holds "{text}"')
def step_api_error(context, status, text):
    assert context.status == status and text in context.body["error"], (context.status, context.body)


@then('the API answers 200 with updates_last_hour {n:d}, window_start "{start}" and window_end "{end}"')
def step_api_number(context, n, start, end):
    assert (context.status, context.body) == (200, {"updates_last_hour": n, "window_start": start,
                                                    "window_end": end}), (context.status, context.body)


@then('GET /current for "{entity}" answers value {value:d} from "{event_id}"')
def step_current(context, entity, value, event_id):
    _send(context, "GET", "/current", params={"entity_id": entity})
    assert context.status == 200 and context.body["value"] == value and context.body["last_event_id"] == event_id, (
        context.body)


@then('the table holds the items "{keys}"')
def step_items(context, keys):
    assert sorted(i["pk"] for i in support.items(context.names["table"])) == _lst(keys)


@then("the table holds no item")
def step_no_items(context):
    assert support.items(context.names["table"]) == []


@then('the counter "{pk}" is {n:d}')
def step_counter(context, pk, n):
    (item,) = [i for i in support.items(context.names["table"]) if i["pk"] == pk]
    assert int(item["n"]) == n, item


@then("each event item and its counter were written in one transaction")
def step_transactions(context):
    made = context.calls.of("TransactWriteItems")
    assert len(made) == len(context.sent)
    for call, update in zip(made, context.sent):
        put, counter = call["TransactItems"]
        assert put["Put"]["Item"]["pk"]["S"] == f"event#{update['event_id']}"
        assert put["Put"]["ConditionExpression"] == "attribute_not_exists(pk)"
        assert counter["Update"]["Key"]["pk"]["S"] == f"minute#{update['event_time'][:16]}"


@then("each event item expires {days:d} days after its event time")
def step_expires(context, days):
    events = [i for i in support.items(context.names["table"]) if i["pk"].startswith("event#")]
    for item in events:
        at = dt.datetime.fromisoformat(item["event_time"].replace("Z", "+00:00")).timestamp()
        assert int(item["expires_at"]) == int(at) + days * 86400, item


@then("the function read {n:d} counters in one BatchGetItem call, eventually consistent")
def step_batch_get(context, n):
    (made,) = context.calls.of("BatchGetItem")
    (request,) = made["RequestItems"].values()
    assert len(request["Keys"]) == n and all(k["pk"]["S"].startswith("minute#") for k in request["Keys"])
    assert not request.get("ConsistentRead")


@then("the function made no Scan and no Query")
def step_no_scan(context):
    assert not {"Scan", "Query"} & set(context.calls.names()), context.calls.names()


@then('the table "{table}" is on demand, with point-in-time recovery on and time to live on "{attribute}"')
def step_table_settings(context, table, attribute):
    ddb = support.client("dynamodb")
    assert context.calls.of("CreateTable", "dynamodb")[0]["BillingMode"] == "PAY_PER_REQUEST"
    backups = ddb.describe_continuous_backups(TableName=table)["ContinuousBackupsDescription"]
    assert backups["PointInTimeRecoveryDescription"]["PointInTimeRecoveryStatus"] == "ENABLED"
    ttl = ddb.describe_time_to_live(TableName=table)["TimeToLiveDescription"]
    assert (ttl["TimeToLiveStatus"], ttl["AttributeName"]) == ("ENABLED", attribute), ttl


@then('the function "{name}" was created from "{file}" alone, with "{runtime}" on "{arch}", {mb:d} MB and a '
      "timeout of {seconds:d} seconds")
def step_function(context, name, file, runtime, arch, mb, seconds):
    (made,) = context.calls.of("CreateFunction")
    assert (made["FunctionName"], made["Runtime"], made["Architectures"], made["MemorySize"], made["Timeout"]) == (
        name, runtime, [arch], mb, seconds), made
    with zipfile.ZipFile(io.BytesIO(made["Code"]["ZipFile"])) as z:
        assert z.namelist() == [file]
        assert z.read(file) == (ROOT / "rep-operational-first" / file).read_bytes()
    assert made["Environment"]["Variables"] == {"TABLE": name.replace("-api", "-updates")}


@then('its URL takes signed requests only: "{auth}"')
def step_url_auth(context, auth):
    (made,) = context.calls.of("CreateFunctionUrlConfig")
    assert made["AuthType"] == auth, made


@then("every request of the run to the URL was signed, in batches of at most {n:d} updates")
def step_requests_signed(context, n):
    posts = [r for r in support.URL_REQUESTS if r["method"] == "POST"]
    assert support.URL_REQUESTS and all(r["signed"] and r["status"] == 200 for r in support.URL_REQUESTS)
    assert [r["updates"] for r in posts] == [100, 100, 100, 5] and max(r["updates"] for r in posts) <= n, posts
    assert all(r["package"] == ["handler.py"] for r in support.URL_REQUESTS)


@then('the log group "{name}" keeps its lines {days:d} days')
def step_log_days(context, name, days):
    (group,) = support.client("logs").describe_log_groups(logGroupNamePrefix=name)["logGroups"]
    assert group["retentionInDays"] == days, group


@then("the URL answers 403 and the function does not run")
def step_forbidden(context):
    assert context.status == 403 and "package" not in support.URL_REQUESTS[-1], support.URL_REQUESTS[-1]


@then('the role "{role}" may get, put, update, batch-get and condition-check items of the table "{table}" and '
      'write the log group "{group}"')
def step_api_role(context, role, table, group):
    context.role = role
    found = {(a, s["Resource"]) for s in support.policy(role) for a in s["Action"]}
    arn = f"arn:aws:dynamodb:ca-central-1:{ACCOUNT}:table/{table}"
    logs = f"arn:aws:logs:ca-central-1:{ACCOUNT}:log-group:{group}:*"
    assert found == {("dynamodb:GetItem", arn), ("dynamodb:PutItem", arn), ("dynamodb:UpdateItem", arn),
                     ("dynamodb:BatchGetItem", arn), ("dynamodb:ConditionCheckItem", arn),
                     ("logs:CreateLogStream", logs), ("logs:PutLogEvents", logs)}, found


@then('the function "{name}" runs as "{arn}"')
def step_function_role(context, name, arn):
    (made,) = context.calls.of("CreateFunction")
    assert (made["FunctionName"], made["Role"]) == (name, arn), made["Role"]


@then('a full export of the table "{table}" went to the bucket "{bucket}" under "{prefix}"')
def step_export(context, table, bucket, prefix):
    (made,) = context.calls.of("ExportTableToPointInTime")
    assert made["TableArn"].endswith(f":table/{table}") and (made["S3Bucket"], made["S3Prefix"]) == (bucket, prefix)
    assert (made["ExportType"], made["ExportFormat"]) == ("FULL_EXPORT", "DYNAMODB_JSON"), made


@then('the Athena table "{table}" was made anew over the data folder of that export')
def step_export_table(context, table):
    sql = [q["QueryString"] for q in context.calls.of("StartQueryExecution")]
    bucket = context.calls.of("ExportTableToPointInTime")[0]["S3Bucket"]
    assert sql[0] == f"DROP TABLE IF EXISTS {table}", sql
    assert sql[1].startswith(f"CREATE EXTERNAL TABLE {table} ")
    assert re.search(rf"LOCATION 's3://{bucket}/exports/AWSDynamoDB/[^/]+/data/'$", sql[1]), sql[1]


@then('the count Athena was asked for is of items of kind "{kind}" with event times from "{start}" up to "{end}"')
def step_export_query(context, kind, start, end):
    sql = context.calls.of("StartQueryExecution")[-1]["QueryString"]
    assert "count(*)" in sql and f"Item.kind.S = '{kind}'" in sql, sql
    assert f"Item.event_time.S >= '{start}'" in sql and f"Item.event_time.S < '{end}'" in sql, sql


@given('DynamoDB answers "{code}" to the first {n:d} export requests')
def step_export_refused(context, code, n):
    context.calls.refuse("ExportTableToPointInTime", code, times=n)


@then("the recipe asked for the export {n:d} times, {seconds:d} seconds apart")
def step_export_tries(context, n, seconds):
    assert len(context.calls.of("ExportTableToPointInTime")) == n and context.sleeps.count(seconds) == n - 1, (
        context.sleeps)


# ---- generative AI first

@given('the gateway settings COURSE_GATEWAY_URL "{url}" and KB_MCP_TOKEN "{token}"')
def step_gateway(context, url, token):
    os.environ.update(COURSE_GATEWAY_URL=url, KB_MCP_TOKEN=token)


@given('the model calls count_updates for the window and then replies "{reply}"')
def step_model(context, reply):
    support.SCRIPT[:] = [{"tool": "count_updates", "arguments": WINDOW}, {"reply": reply}]


@given('the model calls count_updates with start "{start}" and end "{end}", and then replies "{reply}"')
def step_model_window(context, start, end, reply):
    support.SCRIPT[:] = [{"tool": "count_updates", "arguments": {"start": start, "end": end}}, {"reply": reply}]


@given('the model calls count_updates for the window and then replies with no text and finish_reason "length"')
def step_model_cap_used_up(context):
    support.SCRIPT[:] = [{"tool": "count_updates", "arguments": WINDOW}, {"cap_used_up": True}]


@given('the model calls count_updates with arguments cut at the token cap, and then replies "{reply}"')
def step_model_cut_tool_call(context, reply):
    support.SCRIPT[:] = [{"cut_tool_call": True}, {"reply": reply}]


@given("the model calls count_updates for the window in every answer")
def step_model_loops(context):
    support.SCRIPT[:] = [{"tool": "count_updates", "arguments": WINDOW}]


@given('the model calls the tool "{name}" and then replies "{reply}"')
def step_model_other_tool(context, name, reply):
    support.SCRIPT[:] = [{"tool": name, "arguments": WINDOW}, {"reply": reply}]


def _gateway():
    (gateway,) = support.GATEWAYS
    return gateway


@then('the gateway client was made with the URL "{url}" and the token "{token}"')
def step_gateway_made(context, url, token):
    made = _gateway().made_with
    assert (made["base_url"], made["api_key"]) == (url, token), made


@then('the system prompt names the window "{start}" to "{end}"')
def step_system_prompt(context, start, end):
    system = _gateway().calls[0]["messages"][0]
    assert system["role"] == "system" and f"start '{start}' and end '{end}'" in system["content"], system


@then('the user message is "{text}"')
def step_user_message(context, text):
    assert _gateway().calls[0]["messages"][1] == {"role": "user", "content": text}


@then('the model was offered the tools "{names}"')
def step_tools(context, names):
    assert all([t["function"]["name"] for t in c["tools"]] == _lst(names) for c in _gateway().calls)


@then('every model call asked for the model "{model}" with at most {n:d} output tokens')
def step_model_settings(context, model, n):
    assert all((c["model"], c["max_tokens"]) == (model, n) for c in _gateway().calls), _gateway().calls


@then("the model was called {n:d} times")
def step_model_calls(context, n):
    assert len(_gateway().calls) == n


def _after_table(context) -> list[tuple[str, dict]]:
    """The DynamoDB calls after the offline table and its export were built (the build ends with a Scan)."""
    made = [(op, p) for (op, p), s in zip(context.calls.made, context.calls.services) if s == "dynamodb"]
    last_scan = max(i for i, (op, _) in enumerate(made) if op == "Scan")
    return made[last_scan + 1:]


@then('after the table was built, the only DynamoDB calls are BatchGetItem, of keys that start with "{start}"')
def step_tool_reads(context, start):
    made = _after_table(context)
    assert made and {op for op, _ in made} == {"BatchGetItem"}, [op for op, _ in made]
    keys = [k["pk"]["S"] for _, p in made for r in p["RequestItems"].values() for k in r["Keys"]]
    assert len(keys) == 60 and all(k.startswith(start) for k in keys)


@then("after the table was built, no DynamoDB call was made")
def step_tool_no_reads(context):
    assert _after_table(context) == []


@then('the policy for a deployed tool allows "{action}" on the table "{table}" and nothing else')
def step_tool_policy(context, action, table):
    import genai

    assert genai.tool_policy(table, ACCOUNT)["Statement"] == [{
        "Effect": "Allow", "Action": [action], "Resource": f"arn:aws:dynamodb:ca-central-1:{ACCOUNT}:table/{table}"}]


@then('the tool result the model got is an error that holds "{text}"')
def step_tool_error(context, text):
    results = [json.loads(m["content"]) for m in _gateway().calls[-1]["messages"] if m["role"] == "tool"]
    assert results and text in results[0].get("error", ""), results


@then("the genai run created nothing and wrote nothing to the table")
def step_genai_reads_only(context):
    made = set(context.calls.pairs()[context.marks[-1]:])
    assert made == {("sts", "GetCallerIdentity"), ("dynamodb", "BatchGetItem")}, made


@then('the view "{view}" was made over the export, and the count was asked of it')
def step_view(context, view):
    sql = [p["QueryString"] for (op, p) in context.calls.made[context.marks[-1]:] if op == "StartQueryExecution"]
    assert sql[0].startswith(f"CREATE OR REPLACE VIEW {view} AS SELECT") and "FROM updates_export" in sql[0], sql
    assert f"FROM {view} WHERE event_time >= '{WINDOW['start']}' AND event_time < '{WINDOW['end']}'" in sql[1], sql


# ---- clean-up

@given('everything the three representatives create exists for PREFIX "{prefix}"')
def step_everything(context, prefix):
    os.environ["PREFIX"] = prefix
    step_firehose_files(context, 2)
    step_athena(context, 143, 9366)
    for script, args in (("rep-lakehouse-first/run.py", "--target aws"),
                         ("rep-operational-first/run.py", "--target aws --skip-export"),
                         ("rep-genai-first/run.py", "--target aws --llm stub")):
        support.run_script(context, script, args.split())
        assert context.exit_code == 0, (script, context.printed[-1500:], context.error)
    bucket = f"{prefix}-lake-{ACCOUNT}"
    for key in ("updates/dt=2026-11-12/a.parquet", "exports/AWSDynamoDB/01/data/a.json.gz", "athena-results/q.csv"):
        support.client("s3").put_object(Bucket=bucket, Key=key, Body=b"x")
    found = support.inventory()
    assert all(len(found[k]) == 1 for k in ("streams", "firehose", "tables", "functions", "log groups", "buckets",
                                            "glue", "workgroups")) and len(found["roles"]) == 2, found
    assert (context.tmp / ".local").is_dir()
    context.calls.rules.clear()
    context.calls.clear_made()
    context.printed, context.runs = "", []
    context.sleeps.clear()


@then('the folder ".local" is gone')
def step_local_gone(context):
    assert not (context.tmp / ".local").exists()


@given('the stream "{stream}", the table "{table}" and the bucket "{bucket}" of other owners exist')
def step_others(context, stream, table, bucket):
    support.client("kinesis").create_stream(StreamName=stream, ShardCount=1)
    support.client("dynamodb").create_table(
        TableName=table, BillingMode="PAY_PER_REQUEST", KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}])
    support.client("s3").create_bucket(Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": "ca-central-1"})
    context.calls.clear_made()


@then('the account has exactly the stream "{stream}", the table "{table}" and the bucket "{bucket}" left')
def step_others_left(context, stream, table, bucket):
    found = support.inventory()
    assert (found.pop("streams"), found.pop("tables"), found.pop("buckets")) == ([stream], [table], [bucket])
    assert not any(found.values()), found


@given('the role "{role}" does not carry the tag "{key}" = "{value}"')
def step_untag(context, role, key, value):
    support.client("iam").untag_role(RoleName=role, TagKeys=[key])
    context.calls.clear_made()


@then('the account still has the role "{role}"')
def step_role_left(context, role):
    assert support.inventory()["roles"] == [role], support.inventory()["roles"]


@then('the account still has the stream "{stream}"')
def step_stream_left(context, stream):
    assert support.inventory()["streams"] == [stream]


@given("the stream, the Firehose stream and the table are being deleted")
def step_being_deleted(context):
    context.calls.answer("DescribeDeliveryStream", {"DeliveryStreamDescription": {"DeliveryStreamStatus": "DELETING"}})
    context.calls.answer("DescribeStreamSummary", {"StreamDescriptionSummary": {"StreamStatus": "DELETING"}})
    context.calls.answer("DescribeTable", {"Table": {"TableStatus": "DELETING"}})


@then("no call was made to delete the stream, the Firehose stream or the table again")
def step_not_again(context):
    again = {("kinesis", "DeleteStream"), ("firehose", "DeleteDeliveryStream"), ("dynamodb", "DeleteTable")}
    assert not again & set(context.calls.pairs()), again & set(context.calls.pairs())


@then("no call was made to AWS")
def step_no_calls(context):
    assert context.calls.made == [], context.calls.names()

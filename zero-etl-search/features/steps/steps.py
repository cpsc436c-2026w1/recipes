"""Steps of the scenarios in features/. They drive the entry points (main.run.main, main.migrate.main,
clean_up.main, worker.main, zero_etl.main, and the Lambda entry point main/handler.py handler()); features/support.py
holds the stand-ins."""

from __future__ import annotations

import importlib
import io
import json
import os
import random
import socket
import time
import zipfile

import boto3
from behave import given, then, when

import support
from support import UrlIndex, lines_of, patch, run, strip_scores

from main.config import ROOT, Settings


def c(svc):
    return boto3.client(svc, region_name=os.environ.get("AWS_REGION", "ca-central-1"))


# ---- the function at its entry point ----------------------------------------------------------------------------

@given('the function\'s index is the recipe\'s stand-in index "{name}"')
def function_index(context, name):
    from main.mappings import index_body
    from main.standin import StandInIndex

    os.environ["ENDPOINT"], os.environ["INDEX"], os.environ["INDEX_VERSION"] = (
        "https://c-test.ca-central-1.aoss.amazonaws.com", name, "1")
    context.add_cleanup(lambda: [os.environ.pop(k, None) for k in ("ENDPOINT", "INDEX", "INDEX_VERSION")])
    context.index = StandInIndex(name, index_body(Settings()))
    context.url = UrlIndex(context.index)
    context.jitter = 0.0
    context.time_left = 60.0
    context.seq = 0
    context.logged = []


def _handler(context):
    """main.handler loaded afresh, so its defaults (sleep, jitter) are the scenario's; urlopen is the stand-in."""
    import urllib.request

    patch(context, urllib.request, "urlopen", context.url.urlopen)
    patch(context, time, "sleep", lambda s: (context.sleeps.append(s), _spend(context, s)))
    patch(context, random, "random", lambda: context.jitter)
    import builtins

    patch(context, builtins, "print", lambda *a, **k: context.logged.append(" ".join(map(str, a))))
    from main import handler

    h = importlib.reload(handler)
    context.add_cleanup(importlib.reload, handler)
    return h


def _spend(context, s):
    context.time_left -= s


class _Context:
    def __init__(self, context):
        self.c = context

    def get_remaining_time_in_millis(self):
        return int(self.c.time_left * 1000)


def _record(context, event, item_id, name="Item", price=10.0):
    from boto3.dynamodb.types import TypeSerializer
    from decimal import Decimal

    ser = TypeSerializer()
    context.seq += 1
    item = {"id": item_id, "name": name, "description": "d", "category": "gear", "brand": "B",
            "price_cad": Decimal(str(price)) if isinstance(price, float) else price, "in_stock": True}
    d = {"Keys": {"id": {"S": item_id}}, "SequenceNumber": f"{context.seq:021d}",
         "ApproximateCreationDateTime": 1763000000}
    if event != "REMOVE":
        d["NewImage"] = {k: ser.serialize(v) for k, v in item.items()}
    return {"eventName": event, "eventSource": "aws:dynamodb", "dynamodb": d}


def _invoke(context, records):
    h = _handler(context)
    context.last_records = records
    try:
        context.result = h.handler({"Records": records}, _Context(context))
        context.raised = None
    except Exception as e:  # noqa: BLE001 - Lambda counts it as a function error
        context.result, context.raised = None, e
    context.results = getattr(context, "results", []) + [context.result]


def _parse_batch(context, text):
    recs = []
    for part in text.split(", "):
        words = part.split()
        event, item = words[0], words[1].strip('"')
        if event == "REMOVE":
            recs.append(_record(context, "REMOVE", item))
        elif "priced" in words:
            price = part.split("priced ")[1].strip('"')
            recs.append(_record(context, event, item, price=price if not price.replace(".", "").isdigit() else float(price)))
        else:
            recs.append(_record(context, event, item, name=part.split('named "')[1].rstrip('"')))
    return recs


@given('the index holds item "{item}"')
def index_holds(context, item):
    _invoke(context, [_record(context, "INSERT", item)])
    context.url.requests.clear()


@given("the index first answers {first}")
def first_answer(context, first):
    context.url.fail = ["timeout" if first == "timeout" else int(first)]


@given("the index answers {status:d} to the first {n:d} requests")
def answers_n(context, status, n):
    context.url.fail = [status] * n


@given("the function's random part of each wait is the largest")
def jitter_max(context):
    context.jitter = 1.0


@given("the function has {s:g} seconds left")
def time_left(context, s):
    context.time_left = float(s)


@when('the function gets an {event:w} of item "{item}" named "{name}" priced {price:f}')
@when('the function gets a {event:w} of item "{item}" named "{name}" priced {price:f}')
def gets_change(context, event, item, name, price):
    _invoke(context, [_record(context, event, item, name, price)])


@when('the function gets a REMOVE of item "{item}"')
def gets_remove(context, item):
    _invoke(context, [_record(context, "REMOVE", item)])


@when("the function gets one batch: {batch}")
def gets_batch(context, batch):
    _invoke(context, _parse_batch(context, batch))


@when("the function gets that batch again")
def again(context):
    _invoke(context, context.last_records)


@then('the index holds {n:d} document, "{item}", named "{name}" priced {price:f}')
def holds_one(context, n, item, name, price):
    assert list(context.index.docs) == [item] and n == 1, context.index.docs
    d = context.index.docs[item]
    assert (d["name"], d["price_cad"]) == (name, price), d


@then("the index holds {n:d} documents")
@then("the index holds {n:d} document")
def holds_n(context, n):
    assert context.index.count() == n, context.index.docs


@then('the document of "{item}" has index_version 1, changed_at and indexed_at, and no "id" field')
def doc_fields(context, item):
    d = context.index.docs[item]
    assert d["index_version"] == 1 and d["changed_at"] == 1763000000 and isinstance(d["indexed_at"], float)
    assert "id" not in d, d


@then('every request went to "{path}" of the collection\'s endpoint, signed for the service "{service}"')
def signed(context, path, service):
    for r in context.url.requests:
        assert r["url"] == os.environ["ENDPOINT"] + path, r["url"]
        auth = r["headers"]["Authorization"]
        assert f"/ca-central-1/{service}/aws4_request" in auth and "x-amz-content-sha256" in auth, auth


@then("the function sent {n:d} request with {a:d} action")
def one_request(context, n, a):
    assert len(context.url.requests) == n
    lines = [json.loads(x) for x in context.url.requests[0]["body"].splitlines() if x.strip()]
    assert sum(1 for x in lines if set(x) & {"index", "delete"} and "_id" in next(iter(x.values()))) == a, lines


@then("the function returned no batch item failure both times")
def none_twice(context):
    assert context.results[-2:] == [{"batchItemFailures": []}] * 2, context.results


@then("the function returned no batch item failure")
def none_once(context):
    assert context.result == {"batchItemFailures": []}, context.result


@then("the function sent {n:d} requests")
@then("the function sent {n:d} request")
def n_sent(context, n):
    assert len(context.url.requests) == n, len(context.url.requests)


@then('the function returned the sequence number of "{item}" as its batch item failure')
def failure_of(context, item):
    seq = next(r["dynamodb"]["SequenceNumber"] for r in context.last_records
               if r["dynamodb"]["Keys"]["id"]["S"] == item)
    assert context.result == {"batchItemFailures": [{"itemIdentifier": seq}]}, (context.result, context.raised)


@then('the function failed with "{text}"')
def failed_with(context, text):
    assert context.raised is not None and text in str(context.raised), context.raised


@then("the function waited once, between {a:g} and {b:g} seconds")
def waited_once(context, a, b):
    assert len(context.sleeps) == 1 and a <= context.sleeps[0] <= b, context.sleeps


@then("the function waited {waits} seconds")
def waited_list(context, waits):
    assert context.sleeps == [float(x) for x in waits.split(", ")], context.sleeps


@then("the function logged one line with records {r:d}, indexed {i:d}, deleted {d:d}, attempts {a:d}")
def logged(context, r, i, d, a):
    [line] = context.logged
    got = json.loads(line)
    assert (got["records"], got["indexed"], got["deleted"], got["attempts"]) == (r, i, d, a), got


# ---- runs of the scripts ----------------------------------------------------------------------------------------

@when("main/run.py runs with no options")
def run_offline(context):
    import main.run as m

    run(context, m.main, [])


@then("the exit code is {code:d}")
def exit_code(context, code):
    assert context.exit_code == code, (context.exit_code, context.error, context.printed[-2500:])


@then('the printed lines are those of "{path}", scores aside')
def same_lines_scores(context, path):
    want = [strip_scores(x) for x in lines_of((ROOT / path).read_text())]
    got = [strip_scores(x) for x in lines_of(context.stdout)]
    assert got == want, "\n".join(got)


@then('the printed lines are those of "{path}"')
def same_lines(context, path):
    assert lines_of(context.stdout) == lines_of((ROOT / path).read_text()), context.stdout


@then('it printed "{text}"')
def printed(context, text):
    assert text in context.printed, context.printed[-2500:]


@given("every network connection fails the scenario")
def no_network(context):
    def refuse(self, address):
        raise AssertionError(f"the run opened a network connection to {address}")

    patch(context, socket.socket, "connect", refuse)


# ---- AWS set-up ---------------------------------------------------------------------------------------------------

@given("AWS is a stand-in")
def aws(context):
    support.use_aws(context)


@given('PREFIX is "{prefix}"')
def prefix(context, prefix):
    os.environ["PREFIX"] = prefix


@given('INDEX_VERSION is "{v}"')
def index_version(context, v):
    os.environ["INDEX_VERSION"] = v


def _setup(context, options="--target aws --setup"):
    import main.run as m

    run(context, m.main, options.split())


@when('main/run.py runs with "{options}"')
def run_options(context, options):
    _setup(context, options)


@given('main/run.py has run with "{options}"')
def has_run(context, options):
    _setup(context, options)
    assert context.exit_code == 0, context.printed[-2500:]
    context.printed = ""
    context.calls.made.clear()
    context.sleeps.clear()


@then('the table "{name}" is billed per request, keyed by "{key}", with a stream of new and old images')
def table_made(context, name, key):
    [p] = [p for p in context.calls.of("CreateTable") if p["TableName"] == name]
    assert p["BillingMode"] == "PAY_PER_REQUEST" and p["KeySchema"] == [{"AttributeName": key, "KeyType": "HASH"}]
    assert p["StreamSpecification"] == {"StreamEnabled": True, "StreamViewType": "NEW_AND_OLD_IMAGES"}


@then('the queue "{name}" keeps messages {s:d} seconds')
def queue_made(context, name, s):
    url = c("sqs").get_queue_url(QueueName=name)["QueueUrl"]
    got = c("sqs").get_queue_attributes(QueueUrl=url, AttributeNames=["MessageRetentionPeriod"])["Attributes"]
    assert got["MessageRetentionPeriod"] == str(s), got


@then('the log group "{name}" keeps logs {d:d} day')
def log_group(context, name, d):
    [g] = [g for g in c("logs").describe_log_groups(logGroupNamePrefix=name)["logGroups"] if g["logGroupName"] == name]
    assert g["retentionInDays"] == d, g


@then("every AWS call went to {region}")
def region(context, region):
    regions = set(context.calls.regions) - {"aws-global"}     # IAM is a global service
    assert regions == {region}, regions


@then('the function "{name}" runs python3.13 on arm64 with 128 MB and a 60 second time-out')
def function_made(context, name):
    f = c("lambda").get_function_configuration(FunctionName=name)
    assert (f["Runtime"], f["Architectures"], f["MemorySize"], f["Timeout"]) == ("python3.13", ["arm64"], 128, 60), f
    context.function = name


@then("its zip holds handler.py only")
def zip_one(context):
    [p] = [p for p in context.calls.of("CreateFunction")]
    assert zipfile.ZipFile(io.BytesIO(p["Code"]["ZipFile"])).namelist() == ["handler.py"]


@then('its environment names the collection\'s endpoint, the index "{index}" and index version "{v}"')
def function_env(context, index, v):
    env = c("lambda").get_function_configuration(FunctionName=context.function)["Environment"]["Variables"]
    assert env["ENDPOINT"].startswith("https://c-recipe-ana-items.") and env["INDEX"] == index
    assert env["INDEX_VERSION"] == v, env


@then('the mapping from the table\'s stream to "{fn}" has batch size {b:d}, batching window {w:d}, bisect on, ReportBatchItemFailures, {r:d} retries, maximum record age {age:d} and starting position {start}')
def mapping_made(context, fn, b, w, r, age, start):
    [p] = [p for p in context.calls.of("CreateEventSourceMapping") if p["FunctionName"] == fn]
    assert p["EventSourceArn"] == c("dynamodb").describe_table(TableName="recipe-ana-items")["Table"]["LatestStreamArn"]
    got = (p["BatchSize"], p["MaximumBatchingWindowInSeconds"], p["BisectBatchOnFunctionError"],
           p["FunctionResponseTypes"], p["MaximumRetryAttempts"], p["MaximumRecordAgeInSeconds"],
           p["StartingPosition"])
    assert got == (b, w, True, ["ReportBatchItemFailures"], r, age, start), got
    context.mapping = p


@then('its on-failure destination is the queue "{name}"')
def mapping_destination(context, name):
    url = c("sqs").get_queue_url(QueueName=name)["QueueUrl"]
    arn = c("sqs").get_queue_attributes(QueueUrl=url, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
    assert context.mapping["DestinationConfig"] == {"OnFailure": {"Destination": arn}}


@then('the collection group "{name}" is NEXTGEN with standby replicas ENABLED and 0 to 2 OCU for indexing and for search')
def group_made(context, name):
    g = context.aoss.groups[name]
    assert g["generation"] == "NEXTGEN" and g["standbyReplicas"] == "ENABLED"
    assert g["capacityLimits"] == {"minIndexingCapacityInOCU": 0, "minSearchCapacityInOCU": 0,
                                   "maxIndexingCapacityInOCU": 2, "maxSearchCapacityInOCU": 2}, g


@then('the collection "{name}" is a SEARCH collection in that group')
def coll_made(context, name):
    col = context.aoss.collections[name]
    assert (col["type"], col["group"], col["params"]["standbyReplicas"]) == ("SEARCH", "recipe-ana-search", "ENABLED")


@then('the index "{name}" was created with dynamic false and index_version 1 in its _meta')
def index_made(context, name):
    m = context.endpoint.indexes[name]["body"]["mappings"]
    assert m["dynamic"] is False and m["_meta"]["index_version"] == 1, m


def _data_policy(context, name="recipe-ana-items"):
    return context.aoss.policies[(name, "data")]


def _role_arn(context):
    return c("iam").get_role(RoleName="recipe-ana-search-indexer-role")["Role"]["Arn"]


def _me():
    return c("sts").get_caller_identity()["Arn"]


@then('the data access policy of "{name}" lets the function\'s role write documents in "{index}" only')
def writer(context, name, index):
    st = [s for s in _data_policy(context, name) if _role_arn(context) in s["Principal"]]
    assert len(st) == 1 and st[0]["Principal"] == [_role_arn(context)]
    assert st[0]["Rules"] == [{"ResourceType": "index", "Resource": [f"index/{name}/{index}"],
                               "Permission": ["aoss:WriteDocument"]}], st


@then('it lets the identity that ran the script create, describe, read and delete "{index}" and describe the collection')
def owner(context, index):
    [st] = [s for s in _data_policy(context) if _me() in s["Principal"]]
    rules = {r["ResourceType"]: (r["Resource"], sorted(r["Permission"])) for r in st["Rules"]}
    assert rules == {"index": ([f"index/recipe-ana-items/{index}"], sorted(
        ["aoss:CreateIndex", "aoss:DescribeIndex", "aoss:ReadDocument", "aoss:DeleteIndex"])),
        "collection": (["collection/recipe-ana-items"], ["aoss:DescribeCollectionItems"])}, rules


@then('no resource in the data access policy or in the function role\'s policy has a "*" outside a path')
def no_wildcard(context):
    for st in _data_policy(context):
        for r in st["Rules"]:
            assert all("*" not in x for x in r["Resource"]), r
    doc = c("iam").get_role_policy(RoleName="recipe-ana-search-indexer-role", PolicyName="least-privilege")
    for st in doc["PolicyDocument"]["Statement"]:
        for res in ([st["Resource"]] if isinstance(st["Resource"], str) else st["Resource"]):
            assert res != "*" and ":*:" not in res, res     # a trailing /* or :* inside one resource is a path


@then('the function role may call the data API of the collection "{name}" only')
def role_aoss(context, name):
    doc = c("iam").get_role_policy(RoleName="recipe-ana-search-indexer-role", PolicyName="least-privilege")
    [st] = [s for s in doc["PolicyDocument"]["Statement"] if s["Action"] == ["aoss:APIAccessAll"]]
    assert st["Resource"].endswith(f"collection/c-{name}"), st


@given('the function\'s probe answers "cannot write" twice')
def probe_twice(context):
    context.calls.invoke_answers = [{"ok": False, "detail": "HTTP 403"}] * 2


@then("the probe was invoked {n:d} times, every {s:d} seconds, before the mapping was created")
def probe_order(context, n, s):
    names = context.calls.names()
    invokes = [i for i, x in enumerate(names) if x == "Invoke"]
    assert len(invokes) == n and invokes[-1] < names.index("CreateEventSourceMapping"), names
    assert context.sleeps.count(s) >= n - 1


@given("the collection's endpoint answers {status:d} to the first {n:d} requests")
def endpoint_denies(context, status, n):
    context.endpoint.deny = [status] * n


@then("the index create was refused and tried again after {s:d} seconds, {n:d} times")
def index_retried(context, s, n):
    tries = [x for x in context.printed.splitlines() if "refused" in x and f"trying again in {s} s" in x]
    assert len(tries) == n, context.printed


@given('SQS refuses CreateQueue with "{code}" twice')
def sqs_refuses(context, code):
    context.calls.refuse("CreateQueue", "QueueDeletedRecently", times=2, response_code=code)


@then("CreateQueue was called {n:d} times, {s:d} seconds apart")
def queue_retried(context, n, s):
    assert len(context.calls.of("CreateQueue")) == n
    assert context.sleeps.count(s) >= n - 1, context.sleeps


@given('a teammate runs the scripts as "{arn}"')
def teammate(context, arn):
    context.first_me = _me() if not hasattr(context, "first_me") else context.first_me
    context.teammate = arn
    role = arn.split("/")[-1]
    real = context.calls

    def answer(params):
        return {"Arn": f"arn:aws:sts::123456789012:assumed-role/{role}/session", "Account": "123456789012",
                "UserId": "AROAEXAMPLE:session"}

    context.as_teammate = True
    _identity(context, answer)


def _identity(context, answer):
    import botocore.client

    inner = botocore.client.BaseClient._make_api_call

    def make_api_call(client, operation, params):
        if operation == "GetCallerIdentity" and context.as_teammate:
            return answer(params)
        return inner(client, operation, params)

    patch(context, botocore.client.BaseClient, "_make_api_call", make_api_call)


@given("I run the scripts as myself again")
def myself(context):
    context.as_teammate = False


@then("the data access policy names the first identity, the teammate and the function's role")
def three_principals(context):
    context.as_teammate = False
    names = {p for s in _data_policy(context) for p in s["Principal"]}
    assert names == {context.first_me, context.teammate, _role_arn(context)}, names


# ---- the move -----------------------------------------------------------------------------------------------------

@then("the endpoint has the indexes {names}")
def endpoint_indexes(context, names):
    want = sorted(x.strip('" ') for x in names.replace(" and ", ", ").split(","))
    assert sorted(context.endpoint.indexes) == want, context.endpoint.indexes


@then('the data access policy names "{a}" and "{b}" for the function\'s role')
def policy_two(context, a, b):
    [st] = [s for s in _data_policy(context) if _role_arn(context) in s["Principal"]]
    assert st["Rules"][0]["Resource"] == [f"index/recipe-ana-items/{a}", f"index/recipe-ana-items/{b}"], st


@then('the function\'s environment names the index "{index}"')
def env_index(context, index):
    env = c("lambda").get_function_configuration(FunctionName="recipe-ana-search-indexer")["Environment"]["Variables"]
    assert env["INDEX"] == index, env


@then("no item was written to the table")
def no_writes(context):
    assert not [op for op in context.calls.names() if op in ("PutItem", "BatchWriteItem", "UpdateItem")]


@given('the table holds the items {items}')
def table_items(context, items):
    t = boto3.resource("dynamodb", region_name="ca-central-1").Table("recipe-ana-items")
    for i in [x.strip('" ') for x in items.replace(" and ", ", ").split(",")]:
        t.put_item(Item={"id": i, "name": i})
    context.calls.made.clear()


@when('main/migrate.py runs with "{options}"')
def migrate(context, options):
    import main.migrate as m

    run(context, m.main, options.split())


@when("main/migrate.py runs with drop-principal for {who}")
def drop_principal(context, who):
    arn = {"the teammate": context.teammate, "myself": context.first_me}.get(who) or _role_arn(context)
    context.who = arn
    migrate(context, f"drop-principal {arn}")


@then("every UpdateItem of the backfill sets index_backfill on its item with the condition attribute_exists(id)")
def backfill_updates(context):
    ups = context.calls.of("UpdateItem")
    assert len(ups) == 3
    for p in ups:
        assert p["UpdateExpression"] == "SET index_backfill = :m" and p["ConditionExpression"] == "attribute_exists(id)"


@then('no statement of the data access policy names "{index}"')
def not_named(context, index):
    for s in _data_policy(context):
        for r in s["Rules"]:
            assert not any(x.endswith("/" + index) for x in r["Resource"]), s


@then("the data access policy {still} names {who}")
def still_names(context, still, who):
    names = {p for s in _data_policy(context) for p in s["Principal"]}
    assert (context.who in names) == (still == "still"), names


# ---- clean-up -----------------------------------------------------------------------------------------------------

@given("ext-embeddings has been set up")
def embeddings_setup(context):
    import worker

    os.environ["GATEWAY_URL"], os.environ["COURSE_MCP_TOKEN"] = "https://gw.example/v1", "tok-made-up"
    run(context, worker.main, ["--setup", "--target", "aws"])
    assert context.exit_code == 0, context.printed[-2000:]
    context.printed = ""


@given('a teammate\'s table "{table}" and queue "{queue}"')
def teammate_things(context, table, queue):
    c("dynamodb").create_table(TableName=table, BillingMode="PAY_PER_REQUEST",
                               KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
                               AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}])
    c("sqs").create_queue(QueueName=queue)


@when('clean_up.py runs with "{options}"')
def clean_options(context, options):
    import clean_up

    run(context, clean_up.main, options.split())


@when("clean_up.py runs")
def clean(context):
    clean_options(context, "")


@given("clean_up.py has run")
def cleaned(context):
    clean(context)
    assert context.exit_code == 0, context.printed
    context.printed = ""


@then('nothing of "{p}" is left: no table, mapping, function, log group, queue, collection, policy, group or tagged role')
def nothing_left(context, p):
    assert not [t for t in c("dynamodb").list_tables()["TableNames"] if t.startswith(p + "-")]
    assert not c("lambda").list_event_source_mappings()["EventSourceMappings"]
    assert not [f for f in c("lambda").list_functions()["Functions"] if f["FunctionName"].startswith(p)]
    assert not c("logs").describe_log_groups(logGroupNamePrefix=f"/aws/lambda/{p}")["logGroups"]
    assert not [q for q in c("sqs").list_queues().get("QueueUrls", []) if f"/{p}-" in q]
    a = context.aoss
    assert not a.collections and not a.groups and not [k for k in a.policies if k[0].startswith(p)]
    assert not [r for r in c("iam").list_roles()["Roles"] if r["RoleName"].startswith(p)]
    assert not c("cloudwatch").describe_alarms(AlarmNamePrefix=p)["MetricAlarms"]


@then("the teammate's table and queue are still there")
def teammate_kept(context):
    assert "recipe-bo-items" in c("dynamodb").list_tables()["TableNames"]
    assert [q for q in c("sqs").list_queues()["QueueUrls"] if q.endswith("/recipe-bo-search-indexer-failed")]


@then("no delete call was made")
def no_delete(context):
    assert not [op for op in context.calls.names() if op.startswith("Delete")], context.calls.names()


@then('it printed no "removed" line and no "FAILED" line')
def no_removed(context):
    assert not [x for x in context.printed.splitlines() if x.startswith(("removed", "FAILED"))], context.printed


@given('Lambda refuses DeleteFunction with "{code}"')
def refuse_delete(context, code):
    context.calls.refuse("DeleteFunction", code)


@then('it printed a "FAILED" line for "{what}" with "{code}"')
def failed_line(context, what, code):
    lines = [x for x in context.printed.splitlines() if x.startswith("FAILED") and what in x]
    assert len(lines) == 1 and code in lines[0], context.printed


@then("the table, the queue and the collection are gone")
def gone(context):
    assert "recipe-ana-items" not in c("dynamodb").list_tables()["TableNames"]
    assert not [q for q in c("sqs").list_queues().get("QueueUrls", []) if "recipe-ana-search-indexer-failed" in q]
    assert "recipe-ana-items" not in context.aoss.collections


@given('OpenSearch Ingestion refuses GetPipeline with "{code}" and the message "{message}"')
def osis_refuses(context, code, message):
    context.calls.refuse("GetPipeline", code, message=message)


@given('the function "{fn}" was deleted and its mapping left')
def fn_deleted(context, fn):
    c("lambda").delete_function(FunctionName=fn)
    assert c("lambda").list_event_source_mappings()["EventSourceMappings"]


@then("no event source mapping is left")
def no_mapping(context):
    assert not c("lambda").list_event_source_mappings()["EventSourceMappings"]


@given('a role "{name}" without the tag recipe=zero-etl-search')
def untagged_role(context, name):
    c("iam").create_role(RoleName=name, AssumeRolePolicyDocument="{}")


@then('the role "{name}" is still there')
def role_kept(context, name):
    assert c("iam").get_role(RoleName=name)


@then('every name the set-up created starts with "{p}"')
def names_prefix(context, p):
    created = []
    for svc, op, params in context.calls.made:
        if op.startswith("Create") or op == "PutRolePolicy":
            for key in ("TableName", "QueueName", "RoleName", "FunctionName", "logGroupName", "name"):
                if key in params:
                    created.append(params[key])
    assert created and all(x.startswith(p) or x.startswith(f"/aws/lambda/{p}") for x in created), created


# ---- extensions ---------------------------------------------------------------------------------------------------

@when('ext-embeddings/worker.py runs with "{options}"')
def worker_options(context, options):
    import worker

    os.environ["GATEWAY_URL"], os.environ["COURSE_MCP_TOKEN"] = "https://gw.example/v1", "tok-made-up"
    run(context, worker.main, options.split())


@when("ext-embeddings/worker.py runs with no options")
def worker_plain(context):
    import worker

    run(context, worker.main, [])


@then('the collection "{name}" is a VECTORSEARCH collection in the group "{group}"')
def vector_coll(context, name, group):
    col = context.aoss.collections[name]
    assert (col["type"], col["group"]) == ("VECTORSEARCH", group), col
    assert context.aoss.groups[group]["generation"] == "NEXTGEN"


@then('the mapping of "{fn}" has {r:d} retries, maximum record age {age:d}, bisect on and the dead-letter queue "{q}" as its on-failure destination')
def enqueue_mapping(context, fn, r, age, q):
    [p] = [p for p in context.calls.of("CreateEventSourceMapping") if p["FunctionName"] == fn]
    url = c("sqs").get_queue_url(QueueName=q)["QueueUrl"]
    arn = c("sqs").get_queue_attributes(QueueUrl=url, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
    assert (p["MaximumRetryAttempts"], p["MaximumRecordAgeInSeconds"], p["BisectBatchOnFunctionError"],
            p["DestinationConfig"]) == (r, age, True, {"OnFailure": {"Destination": arn}}), p


@when("ext-zero-etl/zero_etl.py runs with no options")
def zero_etl_plain(context):
    import zero_etl

    run(context, zero_etl.main, [])


@when("ext-zero-etl/pipeline.py renders the pipeline for the example settings")
def example_pipeline(context):
    import contextlib
    import runpy
    import sys

    import yaml

    out = io.StringIO()
    patch(context, sys, "argv", ["pipeline.py"])           # without --write: print only, write no file
    with contextlib.redirect_stdout(out):
        runpy.run_path(str(ROOT / "ext-zero-etl" / "pipeline.py"), run_name="__main__")
    context.pipeline = yaml.safe_load(out.getvalue())


@then("its sink writes each item under its primary key, with the stream's action, and an external document version")
def sink(context):
    [name] = [k for k in context.pipeline if k != "version"]
    os_sink = context.pipeline[name]["sink"][0]["opensearch"]
    assert os_sink["document_id"] == '${getMetadata("primary_key")}'
    assert os_sink["action"] == '${getMetadata("opensearch_action")}'
    assert os_sink["document_version"] == '${getMetadata("document_version")}'
    assert os_sink["document_version_type"] == "external"


@then("the teammate may create, search and delete the index and may not write documents")
def teammate_rights(context):
    perms = {perm for s in _data_policy(context) if context.teammate in s["Principal"]
             for rule in s["Rules"] if rule["ResourceType"] == "index" for perm in rule["Permission"]}
    assert {"aoss:CreateIndex", "aoss:ReadDocument", "aoss:DeleteIndex"} <= perms, perms
    assert "aoss:WriteDocument" not in perms, perms
@then('the alarm "{name}" watches the {stat} of {ns} {metric} for the {kind} "{dim}" and goes off {how} over {n:d} period of {period:d} seconds')
def alarm(context, name, stat, ns, metric, kind, dim, how, n, period):
    [a] = c("cloudwatch").describe_alarms(AlarmNames=[name])["MetricAlarms"]
    dim_name = {"function": "FunctionName", "queue": "QueueName"}[kind]
    op, threshold = {"above": ("GreaterThanThreshold", None), "at": ("GreaterThanOrEqualToThreshold", None)}[how.split()[0]]
    value = float(how.split()[1])
    got = (a["Statistic"], a["Namespace"], a["MetricName"], a["Dimensions"], a["ComparisonOperator"], a["Threshold"],
           a["EvaluationPeriods"], a["Period"])
    assert got == (stat, ns, metric, [{"Name": dim_name, "Value": dim}], op, value, n, period), got


@then('no alarm of "{p}" is left')
def no_alarm(context, p):
    assert not c("cloudwatch").describe_alarms(AlarmNamePrefix=p)["MetricAlarms"]


@then('the table "{name}" has point-in-time recovery with a recovery period of {days:d} days')
def pitr(context, name, days):
    [p] = [p for p in context.calls.of("UpdateContinuousBackups") if p["TableName"] == name]
    assert p["PointInTimeRecoverySpecification"] == {"PointInTimeRecoveryEnabled": True, "RecoveryPeriodInDays": days}
    got = c("dynamodb").describe_continuous_backups(TableName=name)["ContinuousBackupsDescription"]
    assert got["PointInTimeRecoveryDescription"]["PointInTimeRecoveryStatus"] == "ENABLED", got


@given('the index "{index}" holds {n:d} documents')
def index_docs(context, index, n):
    context.endpoint.indexes[index]["docs"] = {f"D{i}": {} for i in range(n)}


@given("the failure queue holds {n:d} message")
def queue_holds(context, n):
    url = c("sqs").get_queue_url(QueueName="recipe-ana-search-indexer-failed")["QueueUrl"]
    for _ in range(n):
        c("sqs").send_message(QueueUrl=url, MessageBody="{}")


@given("DynamoDB answers UpdateContinuousBackups with ContinuousBackupsUnavailableException {n:d} times")
def pitr_unavailable(context, n):
    context.calls.refuse("UpdateContinuousBackups", "ContinuousBackupsUnavailableException", times=n)


@then("UpdateContinuousBackups was called {n:d} times")
def pitr_calls(context, n):
    assert context.calls.names().count("UpdateContinuousBackups") == n, context.calls.names()

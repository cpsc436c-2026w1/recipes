"""Steps of features/cost_and_teardown.feature."""
from __future__ import annotations

import json
import os
from pathlib import Path

import boto3
from behave import given, then, when

import support

ACCOUNT = "111122223333"
BUCKET = f"t00-lake-{ACCOUNT}"
SHEET = "price sheet, ca-central-1, 2026-11-18"


def c(name):
    return boto3.client(name, region_name="ca-central-1")


def run(context, script, *argv):
    support.run_script(context, script, list(argv))


def doc() -> str:
    return Path(os.environ["RESOURCES_DOC"]).read_text()


def rows(section: str = "") -> list[list[str]]:
    """The table rows of docs/resources.md: the first table, or the one under the heading `section`."""
    text = doc()
    if section:
        text = text.split(f"## {section}", 1)[1].split("\n## ", 1)[0]
    else:
        text = text.split("\n## ", 1)[0]
    lines = [ln for ln in text.splitlines() if ln.startswith("| ") and not ln.startswith("| Type")]
    return [[cell.strip().strip("`") for cell in ln.strip("|").split("|")] for ln in lines]


def row_of(name: str) -> list[str]:
    found = [r for r in rows() + rows("Not in the ledger") if r[1] == name]
    assert found, doc()
    return found[0]


def ledger() -> list[dict]:
    return [json.loads(x) for x in Path(os.environ["LEDGER"]).read_text().splitlines()]


# ---- the demonstration system

@given("no bucket {bucket} yet")
def step_no_bucket(context, bucket):
    assert bucket not in [b["Name"] for b in c("s3").list_buckets()["Buckets"]]


@given("the bucket {bucket} exists before scripts/demo_up.py runs")
def step_bucket_before(context, bucket):
    c("s3").create_bucket(Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": "ca-central-1"})


@when("scripts/demo_up.py creates the demonstration system")
def step_demo_up(context):
    run(context, "scripts/demo_up.py")
    assert context.exit_code == 0, context.printed


@given("the demonstration system has been created")
def step_demo_made(context):
    step_demo_up(context)


@when("scripts/demo_up.py creates the demonstration system and scripts/inventory.py runs")
def step_demo_and_inventory(context):
    step_demo_up(context)
    run(context, "scripts/inventory.py")


CREATE_TAGS = {
    "S3 bucket": ("CreateBucket", lambda p: p["CreateBucketConfiguration"]["Tags"], "Bucket"),
    "Kinesis data stream": ("CreateStream", lambda p: p["Tags"], "StreamName"),
    "Lambda function": ("CreateFunction", lambda p: p["Tags"], "FunctionName"),
    "Lambda event source mapping": ("CreateEventSourceMapping", lambda p: p["Tags"], None),
    "CloudWatch log group": ("CreateLogGroup", lambda p: p["tags"], "logGroupName"),
    "DynamoDB table": ("CreateTable", lambda p: p["Tags"], "TableName"),
    "Athena workgroup": ("CreateWorkGroup", lambda p: p["Tags"], "Name"),
    "IAM role": ("CreateRole", lambda p: p["Tags"], "RoleName"),
}


@then("each of these {n:d} resources was created with the tags Project=436c, Team=t00 and Owner=ana:")
def step_created_tags(context, n):
    assert len(context.table.rows) == n
    for row in context.table:
        op, get, key = CREATE_TAGS[row["type"]]
        calls = [p for p in context.calls.of(op) if key is None or p.get(key) == row["name"]]
        assert len(calls) == 1, (row["name"], context.calls.of(op))
        t = get(calls[0])
        t = {x["Key"]: x["Value"] for x in t} if isinstance(t, list) else t
        assert {k: t.get(k) for k in ("Project", "Team", "Owner")} == {"Project": "436c", "Team": "t00",
                                                                     "Owner": "ana"}, (row["name"], t)


@then('state/ledger.jsonl has {n:d} rows marked "created", the Glue database {db} and its table among them')
def step_ledger(context, n, db):
    created = [r for r in ledger() if r["state"] == "created"]
    assert len(created) == n, created
    assert {("glue-database", db), ("glue-table", f"{db}.trip_events_demo")} <= {(r["type"], r["name"]) for r in created}


# ---- inventory

@given('an SQS queue "{name}" with the tag Team=t00 that is not in the ledger')
def step_queue(context, name):
    c("sqs").create_queue(QueueName=name, tags={"Team": "t00"})


@when("scripts/inventory.py runs")
def step_inventory(context):
    run(context, "scripts/inventory.py")
    assert context.exit_code == 0, context.printed


@then('docs/resources.md has {n:d} rows and {m:d} row under "Not in the ledger": {name}')
def step_doc_rows(context, n, m, name):
    assert len(rows()) == n, rows()
    other = rows("Not in the ledger")
    assert len(other) == m and other[0][1] == name, other


@then('the rows of {a} and {b} say "{x}" and "{y}"')
def step_bills(context, a, b, x, y):
    assert row_of(a)[3] == x and row_of(b)[3] == y, (row_of(a), row_of(b))


@then('the row of {name} says it was found by "{a}" and "{b}"')
def step_found_by(context, name, a, b):
    routes = [s.strip() for s in row_of(name)[5].split(",")]
    assert a in routes and b in routes, row_of(name)


@then('the row of {name} says "{state}"')
def step_row_state(context, name, state):
    assert row_of(name)[2] == state, row_of(name)


# ---- units

@given("the function's log group has these REPORT lines:")
def step_report_lines(context):
    logs = c("logs")
    group = "/aws/lambda/t00-cost-count"
    logs.create_log_group(logGroupName=group)
    logs.create_log_stream(logGroupName=group, logStreamName="s")
    import time
    events = []
    for i, r in enumerate(context.table):
        init = f"\tInit Duration: {r['init_duration_ms']} ms" if r["init_duration_ms"] else ""
        events.append({"timestamp": int(time.time() * 1000) + i, "message":
                       f"REPORT RequestId: {r['request_id']}\tDuration: {r['billed_duration_ms']}.00 ms\t"
                       f"Billed Duration: {r['billed_duration_ms']} ms\tMemory Size: {r['memory_mb']} MB\t"
                       f"Max Memory Used: 70 MB{init}"})
    logs.put_log_events(logGroupName=group, logStreamName="s", logEvents=events)
    context.inventory = [_row("lambda-function", "t00-cost-count")]


def _row(kind, name):
    from inventory import Row
    return Row(kind, name, f"{kind}:{name}", "created", "", ["ledger"])


@given("a Glue run of {n:d} {worker} workers with ExecutionTime {seconds:d} s and no DPUSeconds")
def step_glue_run(context, n, worker, seconds):
    context.glue_run = {"NumberOfWorkers": n, "WorkerType": worker, "ExecutionTime": seconds}
    context.inventory = [_row("glue-job", "t00-job")]

    def runs(answer, params):
        return {"JobRuns": [context.glue_run]}
    context.calls.change("GetJobRuns", runs)
    c("glue").create_job(Name="t00-job", Role="r", Command={"Name": "glueetl", "ScriptLocation": "s3://b/k"})


@given("the inventory lists an Application Load Balancer and the run did not read its LCUs")
def step_alb(context):
    context.inventory = [_row("load-balancer", "t00-alb")]


@when("the run ends")
def step_run_ends(context):
    from datetime import datetime, timedelta, timezone
    import units
    context.units = units.read_run(boto3.session.Session(region_name="ca-central-1"), "run-1",
                                   datetime.now(timezone.utc) - timedelta(hours=1), rows=context.inventory)


def unit(context, name):
    found = [p for p in context.units["parts"] if p.get("part") == name]
    assert found, context.units
    return found[0]


@then('units.json has {n:d} Lambda requests and {gb_s} billed GB-seconds, marked "{how}"')
def step_lambda_units(context, n, gb_s, how):
    r, g = unit(context, "Lambda requests"), unit(context, "Lambda GB-seconds, arm64")
    assert r["units"] == n and abs(g["units"] - float(gb_s)) < 1e-9 and r["how"] == g["how"] == how, (r, g)


@then('units.json has {dpu} DPU-hours for the job, marked "{how}"')
def step_glue_units(context, dpu, how):
    g = unit(context, "Glue DPU-hours")
    assert round(g["units"], 4) == float(dpu) and g["how"] == how, g


@then('units.json has the row "not measured": "{name}"')
def step_not_measured(context, name):
    assert {"not measured": name} in context.units["parts"], context.units


# ---- cost

@given("these units of a run:")
def step_units(context):
    context.parts = [{"part": r["part"], "units": float(r["units"]), "how": "measured",
                      **({"hourly": True, "count": 1} if r["part"] == "Kinesis shard-hours" else {})}
                     for r in context.table]


@given("the units of a run include {n:d} Parameter Store API interactions")
def step_unpriced(context, n):
    context.parts = [{"part": "Lambda requests", "units": 1000, "how": "measured"},
                     {"part": "Parameter Store API interactions", "units": n, "how": "computed"}]


@when("scripts/cost.py runs")
def step_cost(context):
    import cost
    ev = Path(os.environ["EVIDENCE"]) / "run-1"
    if hasattr(context, "parts"):
        ev.mkdir(parents=True, exist_ok=True)
        (ev / "units.json").write_text(json.dumps({"run_id": "run-1", "parts": context.parts}))
        run(context, "scripts/cost.py", "--run-id", "run-1")
        assert context.exit_code == 0, context.printed
        context.cost_md = (ev / "cost.md").read_text()
        context.rows = cost.run_rows(context.parts, cost.prices())
    else:
        hourly = [("Kinesis shard-hours", context.shards, 0.0165)]
        context.thirty = cost.thirty_days(hourly, context.run_cost, 1, 10)
        context.horizon = cost.horizon(hourly, context.run_cost, 1, 10, getattr(context, "busy", 4),
                                       365, getattr(context, "limit", 30.0))


@then("the cost rows are, in USD:")
def step_cost_rows(context):
    got = {r["part"]: r["usd"] for r in context.rows}
    for r in context.table:
        assert f"{got[r['part']]:.6f}" == r["cost"], (r["part"], got)
        assert f"| {r['part']} |" in context.cost_md and r["cost"] in context.cost_md


@then("the total is {usd} USD and {cad} CAD")
def step_total(context, usd, cad):
    assert f"| **Total** | | | **{usd}** | **{cad}** |" in context.cost_md, context.cost_md


@then('each row names its rate and "{sheet}"')
def step_rate_named(context, sheet):
    lines = [ln for ln in context.cost_md.splitlines() if ln.startswith("| ") and "USD per" in ln or "USD each" in ln]
    assert len(lines) == len(context.table.rows if context.table else context.rows) or len(lines) == 5, lines
    assert all(sheet in ln for ln in lines), lines


@then('the cost row of {part} says "{word}"')
def step_unpriced_row(context, part, word):
    line = next(ln for ln in context.cost_md.splitlines() if ln.startswith(f"| {part} |"))
    assert word in line, line


@then("the total is called a subtotal")
def step_subtotal(context):
    assert "**Subtotal" in context.cost_md and "**Total**" not in context.cost_md, context.cost_md


@given("a stream of {n:d} provisioned shard")
def step_shards(context, n):
    context.shards = n


@given("the parts of a run that are not hourly cost {usd:g} USD")
def step_run_cost(context, usd):
    context.run_cost = usd


@given("the stated rate is one run a day and 10 demonstration hours in 30 days")
def step_rate(context):
    pass                       # the defaults of scripts/cost.py: RUNS_PER_DAY=1, DEMO_HOURS=10


@then("the 30 days always on cost {usd} USD, of which {shard} USD is the shard")
def step_30_on(context, usd, shard):
    assert f"{context.thirty['always_on']:.4f}" == usd and f"{context.thirty['always_on_hourly']:.4f}" == shard


@then("the 30 days for demonstrations only cost {usd} USD, of which {shard} USD is the shard")
def step_30_demo(context, usd, shard):
    assert f"{context.thirty['demo']:.4f}" == usd and f"{context.thirty['demo_hourly']:.4f}" == shard


@given("a stream of {n:d} provisioned shard and parts of a run that are not hourly at {usd:g} USD")
def step_shards_and_cost(context, n, usd):
    context.shards, context.run_cost = n, usd


@given("{busy:d} busy months of 30 days with a run a day, {idle:d} idle days, and a cost limit of {limit:g} USD")
def step_year(context, busy, idle, limit):
    assert busy * 30 + idle == 365
    context.busy, context.limit = busy, limit


@then("the year always on costs {usd} USD and {cad} CAD, and passes the cost limit in the 3rd month")
def step_year_on(context, usd, cad):
    h = context.horizon["always_on"]
    assert f"{h['total']:.2f}" == usd and f"{h['total'] * 1.45:.2f}" == cad and h["passes_in"] == 3, h


@then("the year for demonstrations only costs {usd} USD and {cad} CAD, and does not pass it")
def step_year_demo(context, usd, cad):
    h = context.horizon["demo"]
    assert f"{h['total']:.2f}" == usd and f"{h['total'] * 1.45:.2f}" == cad and h["passes_in"] is None, h


# ---- teardown

@given("the demonstration system and its inventory")
def step_demo_and_its_inventory(context):
    step_demo_up(context)
    run(context, "scripts/inventory.py")
    context.printed = ""


@given("a stream {name} created afterwards with the tag Team=t00")
def step_extra_stream(context, name):
    c("kinesis").create_stream(StreamName=name, ShardCount=1)
    c("kinesis").add_tags_to_stream(StreamName=name, Tags={"Team": "t00"})     # moto keeps no tags given at create


@given("the delete of bucket {bucket} is refused with AccessDenied")
def step_refuse_bucket(context, bucket):
    context.calls.refuse("DeleteBucket", "AccessDenied", when=lambda p: p.get("Bucket") == bucket)


@given('an AppSync API "{name}" that carries the tag Team=t00')
def step_appsync(context, name):
    api = c("appsync").create_graphql_api(name=name, authenticationType="API_KEY", tags={"Team": "t00"})
    context.api_id = api["graphqlApi"]["apiId"]


@when("scripts/teardown.sh runs")
def step_teardown(context):
    run(context, "scripts/teardown.py", "--yes")        # a bare run only lists (the clean-up contract)


@then("the first delete is of stream {name}")
def step_first_delete(context, name):
    deletes = [(op, p) for op, p in context.calls.made[context.mark:] if op.startswith("Delete")]
    assert deletes[0] == ("DeleteStream", {"StreamName": name, "EnforceConsumerDeletion": True}), deletes[:3]


@then("the last step is scripts/lake.py down")
def step_last_step(context):
    steps = [ln for ln in context.printed.splitlines() if ln.startswith("step ")]
    assert steps[-1].startswith("step     shared bucket and workgroups"), steps
    import teardown
    assert teardown.steps()[-1]["command"] == "scripts/lake.py down --yes"


ASKS = {"HeadBucket", "DescribeStreamSummary", "GetFunction", "GetEventSourceMapping", "DescribeLogGroups",
        "DescribeTable", "ListWorkGroups", "GetRole", "GetDatabase", "GetTable"}


@then("each of the {n:d} resources is asked for by its own service after the deletes")
def step_asked(context, n):
    made = context.calls.made[context.mark:]
    last_delete = max(i for i, (op, _) in enumerate(made) if op.startswith("Delete"))
    asked = {op for op, _ in made[last_delete + 1:]} & ASKS
    assert len(ASKS) == n and asked == ASKS, ASKS - asked


@when("scripts/teardown.sh runs with --tagged")
def step_teardown_tagged(context):
    run(context, "scripts/teardown.py", "--yes", "--tagged")


@given("the tagging API's GetResources is refused with AccessDenied")
def step_refuse_get_resources(context):
    context.calls.refuse("GetResources", "AccessDenied")
    context.calls.rules.insert(0, context.calls.rules.pop())    # before the rule that adds the streams to its answer


@then("stream {name} is still there")
def step_stream_kept(context, name):
    assert name in c("kinesis").list_streams()["StreamNames"], context.printed


@then("stream {name} is deleted")
def step_stream_gone(context, name):
    assert name not in c("kinesis").list_streams()["StreamNames"], context.printed


@then("the bucket still exists")
def step_bucket_kept(context):
    c("s3").head_bucket(Bucket=BUCKET)
    assert row_of(BUCKET)[6] == "kept", row_of(BUCKET)


@then('it prints "{text}"')
def step_prints(context, text):
    assert text in context.printed, context.printed[-2000:]


@then("it exits with code {code:d}")
def step_exit(context, code):
    assert context.exit_code == code, (context.exit_code, context.printed[-2000:])


@then('each of the {n:d} rows of docs/resources.md says "deleted" with the time its service answered "not found"')
def step_all_deleted(context, n):
    r = rows()
    assert len(r) == n and all(x[6].startswith("deleted 20") for x in r), doc()


@then('docs/resources.md lists "{name}" under "Delete by hand" with its ARN')
def step_by_hand(context, name):
    hand = rows("Delete by hand")
    assert any(x[1] == name and x[2].startswith("arn:aws:appsync:") and context.api_id in x[2] for x in hand), doc()


@then("the API is still there")
def step_api_kept(context):
    c("appsync").get_graphql_api(apiId=context.api_id)

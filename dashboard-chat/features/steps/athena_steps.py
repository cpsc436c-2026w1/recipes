"""Steps for ext-athena-engine (features/athena_engine.feature).

Offline: the query function runs in-process against the stand-in for Athena (ext-athena-engine/standin.py) and
moto's DynamoDB, on a virtual clock (the 18 s scenarios take no wall-clock time). @live: the function in AWS, the
real clock; ext-athena-engine/deploy.py must have run with the data the scenario names.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from behave import given, then, when

from settings import MB, Limits, Names, athena_usd

N = "SIM01 \"three-zone day\""


def _names() -> Names:
    return Names()


def _fill(text: str) -> str:
    n = _names()
    return text.replace("<PREFIX>", n.prefix).replace("<prefix>", n.glue_prefix)


def _live(context) -> bool:
    return "live" in context.scenario.effective_tags


def _offline(context, **athena_kw):
    from standin import Offline, VirtualClock

    context.clock = VirtualClock()
    context.off = Offline(Path(tempfile.mkdtemp(dir=context.tmp)), context.clock, **athena_kw)
    context.engine_a = context.off.engine
    context.model_takes = 0.0


def _live_engine(context, partitions: int):
    import boto3

    from athena_engine import AthenaEngine, LambdaInvoker
    from chat_athena import RealClock

    n = _names()
    found = boto3.client("glue", region_name="ca-central-1").get_partitions(DatabaseName=n.database,
                                                                             TableName="trips")["Partitions"]
    assert len(found) == partitions, (f"trips has {len(found)} partitions; run ext-athena-engine/deploy.py "
                                      f"--data {'ds01' if partitions == 32 else 'sim01'} first")
    context.clock = RealClock()
    context.engine_a = AthenaEngine(LambdaInvoker(n.function))
    context.off = None
    context.model_takes = 0.0
    context.wg_seen = set(_workgroup_queries())


def _workgroup_queries() -> list[str]:
    """The ids of the latest 100 queries of the chat workgroup, newest first (live: what Athena received)."""
    import boto3

    athena, ids, kw = boto3.client("athena", region_name="ca-central-1"), [], {}
    for _ in range(2):
        page = athena.list_query_executions(WorkGroup=_names().workgroup, MaxResults=50, **kw)
        ids += page["QueryExecutionIds"]
        if not page.get("NextToken"):
            break
        kw = {"NextToken": page["NextToken"]}
    return ids


def _usage_today(mb: int | None = None) -> int | None:
    """Live: read today's billed MB in <PREFIX>-chat-usage, or set it to mb (None: delete today's item)."""
    from datetime import datetime, timezone

    import boto3

    ddb, table = boto3.client("dynamodb", region_name="ca-central-1"), _names().usage_table
    key = {"usage_date": {"S": datetime.now(timezone.utc).strftime("%Y-%m-%d")}}
    if mb is None:
        item = ddb.get_item(TableName=table, Key=key, ConsistentRead=True).get("Item")
        return int(item["billed_bytes"]["N"]) if item and "billed_bytes" in item else None
    ddb.update_item(TableName=table, Key=key, UpdateExpression="SET billed_bytes = :b",
                    ExpressionAttributeValues={":b": {"N": str(mb * MB)}})
    return mb


@given(f"table trips holds the simulated data set {N}")
def holds_sim01(context):
    if _live(context):
        _live_engine(context, 1)
    else:
        _offline(context)


@given(f"table trips holds the simulated data set {N}, and every query would scan {{mb:d}} MB")
def holds_sim01_scanning(context, mb):
    _offline(context, scanned_bytes=mb * MB)


@given(f"table trips holds the simulated data set {N}, and Athena keeps every query running")
def holds_sim01_never(context):
    _offline(context, takes_s=None)


@given(f"table trips holds the simulated data set {N}, and Athena takes {{s:d}} s for each query")
def holds_sim01_slow(context, s):
    _offline(context, takes_s=float(s))


@given("table trips holds DS01")
def holds_ds01(context):
    _live_engine(context, 32)


@given("the model's tool calls are:")
def tool_calls(context):
    context.sqls = [row["sql"] for row in context.table]


@given("the stand-in model takes {s:d} s for each reply")
def model_takes(context, s):
    context.model_takes = float(s)


@when('the analyst asks the Athena chat "{question}"')
def analyst_asks(context, question):
    from chat_athena import ask
    from main.traces import LocalTraces
    from standin import ScriptedModel

    context.model_a = ScriptedModel(context.sqls, context.clock, takes_s=context.model_takes)
    traces = LocalTraces(context.tmp / "athena-traces.jsonl")
    context.answer = ask(question, model=context.model_a, engine=context.engine_a, clock=context.clock,
                         traces=traces)
    context.trace = traces.all()[-1]


def _tool_steps(context):
    return [s for s in context.answer.steps if s.kind == "tool_call"]


@then('the rows are "{text}"')
def rows_are(context, text):
    shown = "; ".join(", ".join(f"{c}: {v}" for c, v in zip(context.answer.columns, r)) for r in context.answer.rows)
    assert shown == text, (shown, context.answer.status, context.answer.error)


@then('the query ran in workgroup "{wg}" with database "{db}"')
def ran_in(context, wg, db):
    q = context.answer.queries[-1]
    rec = next(r for r in context.engine_a.queries if r.get("query_execution_id") == q["query_execution_id"])
    assert (rec["workgroup"], rec["database"]) == (_fill(wg), _fill(db)), rec


@then("each of the {n:d} tool results of the Athena chat is a rejection")
def all_rejected(context, n):
    tools = _tool_steps(context)
    assert len(tools) == n and all(s.status == "rejected" for s in tools), [(s.status, s.detail) for s in tools]


@then("Athena received {n:d} queries")
@then("Athena received {n:d} query")
def athena_received(context, n):
    if context.off is None:  # live: the queries the workgroup received since the Given
        new = [q for q in _workgroup_queries() if q not in context.wg_seen]
        assert len(new) == n, new
        return
    assert len(context.off.athena.started) == n, context.off.athena.started


@then("Athena received {n:d} queries and {m:d} calls of StopQueryExecution")
def received_and_stopped(context, n, m):
    assert (len(context.off.athena.started), len(context.off.athena.stops)) == (n, m)


@then("Athena cancels the query")
def athena_cancels(context):
    assert context.answer.queries and context.answer.queries[-1]["state"] == "CUTOFF", context.answer.queries


@then('the tool result is "{text}"')
@then('the {nth:d}th tool result is "{text}"')
def tool_result_is(context, text, nth=None):
    step = _tool_steps(context)[(nth or 1) - 1]
    assert step.status == "rejected" and step.detail.endswith(f" -- {text}"), step.detail


@then("the question's trace has billed Data scanned {mb:d} MB")
def trace_billed(context, mb):
    assert context.trace["billed_bytes"] == mb * MB, context.trace["billed_bytes"]


@then('the reason is "{reason}"')
def reason_is(context, reason):
    assert context.answer.reason == reason, (context.answer.reason, context.answer.status)


@then("the question ended within {s:g} s")
def ended_within(context, s):
    assert context.answer.latency_ms / 1000 <= s, context.answer.latency_ms


@then("the second model call starts at {start:d} s with a timeout of {timeout:d} s")
def second_call(context, start, timeout):
    c = context.model_a.calls[1]
    assert abs(c["start"] - context.clock.t0 - start) < 0.01 and abs(c["timeout_s"] - timeout) < 0.01, c


@then("the second model call is cut off at {at:d} s")
def cut_off(context, at):
    assert abs(context.model_a.calls[1]["cut_off_at"] - context.clock.t0 - at) < 0.01


# ---- R130 live: response time ----

@when('run_athena.py --measure asks the 10 questions of the eval "trip-counts" 3 times each, one at a time')
def measure(context):
    import io
    from contextlib import redirect_stdout

    from run_athena import main as run_main

    out = io.StringIO()
    with redirect_stdout(out):
        context.exit_code = run_main(["--target", "aws", "--measure"])
    context.printed = out.getvalue()


@then("the 29th of the 30 response times, in order, is under 20 s")
def p95_under(context):
    lines = [json.loads(x) for x in context.printed.splitlines() if x.startswith("{")]
    times = sorted(x["latency_ms"] / 1000 for x in lines)
    assert len(times) == 30 and times[28] < 20, times


# ---- R170 ----

@when('the policy of the role "{role}" is written for the bucket "{bucket}"')
def policy_written(context, role, bucket):
    import importlib.util

    spec = importlib.util.spec_from_file_location("athena_deploy", Path(__file__).resolve().parents[2]
                                                  / "ext-athena-engine" / "deploy.py")
    deploy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(deploy)
    assert _fill(role) == _names().role
    context.policy = deploy.reader_policy(_names(), _fill(bucket), "111122223333")


def _statements_with(context, action):
    return [s for s in context.policy["Statement"] if action in s["Action"]]


@then('it allows athena:StartQueryExecution only on the workgroup "{wg}"')
def athena_only(context, wg):
    st = _statements_with(context, "athena:StartQueryExecution")
    assert len(st) == 1 and st[0]["Resource"].endswith(f":workgroup/{_fill(wg)}")


@then("it allows no Glue action that creates, changes or deletes a table")
def no_glue_writes(context):
    actions = {a for s in context.policy["Statement"] for a in s["Action"] if a.startswith("glue:")}
    assert actions <= {"glue:GetDatabase", "glue:GetTable", "glue:GetPartitions"}, actions


@then('it allows s3:PutObject only under "{prefix}"')
def put_only(context, prefix):
    st = _statements_with(context, "s3:PutObject")
    assert len(st) == 1 and st[0]["Resource"].endswith(f"/{prefix}*"), st


@given('the function "{fn}" with its role "{role}"')
def live_function(context, fn, role):
    import boto3

    conf = boto3.client("lambda", region_name="ca-central-1").get_function_configuration(FunctionName=_fill(fn))
    assert conf["Role"].endswith(f"/{_fill(role)}"), conf["Role"]


@when("it is invoked directly with each of these queries:")
def invoke_directly(context):
    import time

    from athena_engine import LambdaInvoker

    inv = LambdaInvoker(_names().function)
    context.direct = []
    for row in context.table:
        out = inv({"sql": row["sql"], "workgroup": _fill(row["workgroup"]), "deadline": time.time() + 12}, 13)
        denied = out["state"] == "DENIED" or ("access" in out.get("message", "").lower()
                                              and "denied" in out.get("message", "").lower())
        got = "succeeded" if out["state"] == "SUCCEEDED" else "access denied" if denied else out["state"]
        context.direct.append((row["answer"], got, out.get("message", "")))


@then("each query gets the answer in its row")
def direct_answers(context):
    assert all(want == got for want, got, _ in context.direct), context.direct


@then("table trips still exists")
def trips_exists(context):
    import boto3

    boto3.client("glue", region_name="ca-central-1").get_table(DatabaseName=_names().database, Name="trips")


# ---- R180 ----

@given("the queries of a question ended like this:")
def queries_ended(context):
    from settings import billed_mb

    context.ended = [{"ran": True, "state": r["state"], "data_scanned_bytes": int(r["data_scanned_mb"]) * MB,
                      "billed_bytes": billed_mb(r["state"], int(r["data_scanned_mb"]) * MB) * MB,
                      "sql": f"query {r['query']}"} for r in context.table]


@when("the question ends")
def question_ends(context):
    from athena_engine import AthenaEngine

    context.usage = AthenaEngine(lambda p, t: {}).usage(context.ended)


@then("its trace item has billed Data scanned {mb:d} MB")
def usage_billed(context, mb):
    assert context.usage["billed_bytes"] == mb * MB, context.usage


@then("its Athena cost is {usd:g} USD")
def usage_cost(context, usd):
    assert round(context.usage["athena_usd"], 6) == usd and round(athena_usd(context.usage["billed_bytes"]), 6) == usd


@then("its model calls' cost is in CAD")
def model_cad(context):
    from chat_athena import AthenaAnswer

    assert "model_cad" in AthenaAnswer.__dataclass_fields__ and "athena_usd" in AthenaAnswer.__dataclass_fields__


# ---- R190 ----

def _typed(table):
    return [[int(v) if v.lstrip("-").isdigit() else v for v in row.cells] for row in table]


@given("the reference rows of a case are:")
def reference_rows(context):
    context.reference = _typed(context.table)


@given("the last query that succeeded returned, after its header row:")
def last_rows(context):
    context.got = _typed(context.table)


@when("the case is scored")
def scored(context):
    from trip_counts import score

    context.correct, context.why = score(context.reference, "answered", context.got, "")


@then("the run is correct")
def run_correct(context):
    assert context.correct, context.why


def _run_eval(context, model):
    from chat_athena import ask
    from trip_counts import load_cases, run_eval

    def one(q):
        a = ask(q, model=model, engine=context.engine_a, clock=context.clock)
        return a.status, a.last_rows, a.reply

    def reference(sql):
        out = context.engine_a.invoke({"sql": sql, "deadline": context.clock.now() + 10}, 11)
        return out["rows"]

    context.verdict = run_eval(one, reference, load_cases(), log=lambda *a: None)


@when('the scripted model answers each of the 10 cases of the eval "trip-counts" 3 times')
def eval_scripted(context):
    from standin import ScriptedModel
    from trip_counts import load_cases

    replies = {c["question"]: [c["reference_sql"]] for c in load_cases()}
    _run_eval(context, ScriptedModel([], context.clock, replies=replies))


@when('the model that the system uses answers each case of the eval "trip-counts" 3 times')
def eval_gateway(context):
    from main.config import Settings
    from main.model import GatewayModel

    from settings import MODEL_MAX_TOKENS

    s = Settings()
    _run_eval(context, GatewayModel(s.gateway_url, s.token, s.chat_model, max_tokens=MODEL_MAX_TOKENS))


@then("at least {n:d} of the 30 runs are correct")
def runs_correct(context, n):
    assert context.verdict["runs_correct"] >= n, context.verdict


@then("at least {n:d} of the 10 cases pass")
def cases_pass(context, n):
    assert context.verdict["cases_passed"] >= n, context.verdict


# ---- R200 ----

@when("a query is asked for with today's billed Data scanned at each of these:")
def daily_cap(context):
    from athena_engine import Deadline
    from main.chat import ToolRefused

    context.cap = []
    live = context.off is None
    before = _usage_today() if live else None  # live: today's real usage, given back after the rows
    try:
        for row in context.table:
            if live:
                _usage_today(int(row["billed today, MB"]))
            else:
                context.off.set_billed_today_mb(int(row["billed today, MB"]))
            context.engine_a.new_question(Deadline(context.clock, Limits().deadline_s))
            try:
                context.engine_a.run("SELECT COUNT(*) FROM trips")
                got = "the query runs"
            except ToolRefused as e:
                got = str(e)
            context.cap.append((row["result"], got))
    finally:
        if live:
            billed_here = 10  # the one query that ran bills Athena's 10 MB minimum
            if before is None:
                _usage_today(billed_here)
            else:
                _usage_today(before // MB + billed_here)


@then("each query gets the result in its row")
def cap_results(context):
    assert all(w == g for w, g in context.cap), context.cap


# ---- R220 ----

@given('the extension\'s resources exist, the workgroup "{wg}" recorded borrowed')
def resources_exist(context, wg):
    import os

    import boto3
    import lake
    from tags import record

    from standin import create_usage_table

    os.environ["LEDGER"] = str(context.tmp / "ledger.jsonl")
    n = _names()
    sess = boto3.Session(region_name="ca-central-1")
    lake.up(sess, log=lambda *a: None)
    context.bucket = lake.bucket_name(sess)
    s3 = sess.client("s3")
    for key in ("chat/trips/requested_day=2024-03-14/part-0.parquet", "raw/trip-events/dt=2024-03-14/a.jsonl"):
        s3.put_object(Bucket=context.bucket, Key=key, Body=b"x")
    sess.client("iam").create_role(RoleName=n.role, AssumeRolePolicyDocument=json.dumps(
        {"Version": "2012-10-17", "Statement": []}))
    create_usage_table(sess.client("dynamodb"), n.usage_table)
    sess.client("logs").create_log_group(logGroupName=n.log_group)
    sess.client("glue").create_database(DatabaseInput={"Name": f"{n.glue_prefix}_chat"})
    sess.client("athena").create_work_group(Name=_fill(wg), Configuration={})
    record("athena-workgroup", _fill(wg), "borrowed")
    context.sess = sess


@when("ext-athena-engine/clean_up.py --yes runs")
def run_clean_up(context):
    import importlib.util

    spec = importlib.util.spec_from_file_location("athena_clean_up", Path(__file__).resolve().parents[2]
                                                  / "ext-athena-engine" / "clean_up.py")
    clean = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(clean)
    context.steps = clean.clean_up(_names(), yes=True, sess=context.sess)
    assert not context.steps.failed, context.steps.failed


@then("the function, its log group, the role, the usage table, the database and the prefix chat/ are gone")
def gone(context):
    s, n = context.sess, _names()
    assert s.client("iam").list_roles()["Roles"] == []
    assert n.usage_table not in s.client("dynamodb").list_tables()["TableNames"]
    assert s.client("logs").describe_log_groups(logGroupNamePrefix=n.log_group)["logGroups"] == []
    assert s.client("glue").get_databases()["DatabaseList"] == []
    keys = [o["Key"] for o in s.client("s3").list_objects_v2(Bucket=context.bucket).get("Contents", [])]
    assert not any(k.startswith("chat/") for k in keys), keys
    assert s.client("lambda").list_functions()["Functions"] == []


@then('the bucket, the prefix raw/trip-events/ and the workgroup "{wg}" are still there')
def kept(context, wg):
    s = context.sess
    s.client("s3").head_bucket(Bucket=context.bucket)
    keys = [o["Key"] for o in s.client("s3").list_objects_v2(Bucket=context.bucket).get("Contents", [])]
    assert keys == ["raw/trip-events/dt=2024-03-14/a.jsonl"]
    s.client("athena").get_work_group(WorkGroup=_fill(wg))

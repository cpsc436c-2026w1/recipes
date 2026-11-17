"""Steps for the three extensions: the answer cache, the guardrails and the red team, spans and alarms."""

from __future__ import annotations

import json
import shutil
import time

import boto3
import duckdb
from behave import given, then, when
from botocore.exceptions import ClientError
from support import (GATEWAY_URL, TOKEN, FailsSomeQuestions, Recording, in_moto, over_stand_in, patch,
                     refuses_first_call, run_captured, set_env)

from main.chat import answer
from main.config import Settings
from main.data import data_version
from main.infra import ensure_table
from main.model import StubModel


# ---- answer cache ----

@given("an empty answer cache")
def empty_cache(context):
    from cache import LocalCache

    context.cache = LocalCache()


@given('the answer cache is the DynamoDB table "{name}"')
def dynamo_cache(context, name):
    from cache import DynamoCache

    in_moto(context)
    ensure_table(name, Settings().region, key="cache_key")
    context.cache = DynamoCache(name, Settings().region)


@given('the answer cache is the DynamoDB table "{name}", which does not exist')
def missing_dynamo_cache(context, name):
    from cache import DynamoCache

    in_moto(context)
    context.cache = DynamoCache(name, Settings().region)


@when('customer "{user}" asks through the cache "{question}"')
def asks_through_cache(context, user, question):
    from cache import answer_version, cached_answer

    def ask(q):
        return answer(q, user_id=user, model=context.model, engine=context.engine, data_version=context.version)

    result, _hit = run_captured(context, cached_answer, question, user_id=user, cache=context.cache,
                                data_version=context.version, ask=ask,
                                answer_version=answer_version(context.model.model))
    context.results.append(result)


@when('customer "{user}" asks through the cache in front of the guardrails "{question}"')
def asks_through_cache_and_guardrails(context, user, question):
    from cache import answer_version, cached_answer
    from guards import guarded_answer

    def ask(q):  # the guarded chat: its engine holds only this customer's rows
        return guarded_answer(q, user_id=user, model=context.model, engine=context.engine,
                              data_version=context.version)[0]

    result, _hit = run_captured(context, cached_answer, question, user_id=user, cache=context.cache,
                                data_version=context.version, ask=ask, per_user_rows=True,
                                answer_version=answer_version(context.model.model))
    context.results.append(result)


@when("{hours:d} hours pass")
def hours_pass(context, hours):
    import cache

    context.clock_ahead = getattr(context, "clock_ahead", 0) + hours * 3600
    ahead = context.clock_ahead

    class Clock:  # the cache module's clock, moved ahead; every other clock is left alone
        @staticmethod
        def time():
            return time.time() + ahead

    patch(context, cache, "time", Clock)


@when('the model changes to "{name}"')
def model_changes(context, name):
    context.model.inner.model = context.model.model = name


@when('the rule "{rule}" is added to the system prompt')
def rule_added(context, rule):
    import main.prompts as prompts

    patch(context, prompts, "SYSTEM_TEMPLATE", prompts.SYSTEM_TEMPLATE + f"\n- {rule}")


@when("cache_run.py runs with the stub LLM")
def cache_run_runs(context):
    import cache_run

    import main.run as run

    # The run reads the scenario's Parquet files and writes its traces to the scenario's temporary folder.
    patch(context, run, "ensure_data", lambda: context.paths)
    patch(context, run, "OUTPUT_DIR", context.tmp)
    context.lookups = []
    real = cache_run.cached_answer

    def recording(question, **kwargs):  # the real look-up, with what cache_run.py passed to it written down
        context.lookups.append(kwargs)
        return real(question, **kwargs)

    patch(context, cache_run, "cached_answer", recording)
    run_captured(context, cache_run.main, ["--target", "local", "--model", "stub"])


@then("every look-up of the run carried the stub model's name and the version of the system prompt")
def lookups_carried_version(context):
    from main.prompts import prompt_version

    expected = f"qwen3.6-35b-a3b:{prompt_version()}"
    assert len(context.lookups) == 18, len(context.lookups)
    assert {kw.get("answer_version") for kw in context.lookups} == {expected}, context.lookups[0]


@when("the Parquet files change")
def parquet_files_change(context):
    changed = context.tmp / "data"
    changed.mkdir()
    paths = {table: changed / path.name for table, path in context.paths.items()}
    shutil.copy(context.paths["stores"], paths["stores"])
    duckdb.connect().execute(
        f"COPY (SELECT * FROM read_parquet('{context.paths['orders']}') WHERE order_id > 1) "
        f"TO '{paths['orders']}' (FORMAT parquet)")
    new = data_version(paths)
    assert new != context.version
    context.version = new


@then('result {n:d} has status "{status}"')
def result_status(context, n, status):
    assert context.results[n - 1]["status"] == status, context.results[n - 1]["status"]


@then("result {n:d} has the rows of result {m:d}")
def result_same_rows(context, n, m):
    assert context.results[n - 1]["rows"] == context.results[m - 1]["rows"] != []


@then("result {n:d} has other rows than result {m:d}")
def result_other_rows(context, n, m):
    assert context.results[n - 1]["rows"] != context.results[m - 1]["rows"]


@then("result {n:d} has cost 0")
def result_cost_zero(context, n):
    r = context.results[n - 1]
    assert (r["cost_cad"], r["tokens_in"], r["tokens_out"]) == (0.0, 0, 0), r


@then("the cache holds {n:d} items")
def cache_holds_items(context, n):
    assert len(context.cache.items) == n, context.cache.items


@then("the cache holds {n:d} item")
def cache_holds_item(context, n):
    assert len(context.cache.items) == n, context.cache.items


# ---- guardrails and the red team ----

@given("the stub LLM with the replies of attacks.toml and normal.toml")
def red_team_stub(context):
    from redteam import load_cases

    context.model = Recording(StubModel({c["text"]: c["stub"] for c in load_cases()}))


@given('the model call of the case "{case}" fails with "{message}"')
def case_fails(context, case, message):
    import redteam

    text = next(c["text"] for c in redteam.load_cases() if c["id"] == case)
    stub = redteam.StubModel
    patch(context, redteam, "StubModel", lambda script: FailsSomeQuestions(stub(script), {text: message}))


@given('the gateway client of the red-team run, over a stand-in that refuses the first call with "{code}" '
       'and Retry-After "{seconds}"')
def red_team_gateway_client(context, code, seconds):
    import redteam

    set_env(context, "GATEWAY_URL", GATEWAY_URL)
    set_env(context, "COURSE_MCP_TOKEN", TOKEN)
    context.gateway_requests, context.waits = [], []
    handler = refuses_first_call(code, None if seconds == "none" else seconds, context.gateway_requests)
    model = over_stand_in(redteam.gateway_model(Settings()), handler)
    model.sleep = context.waits.append   # the scenario's clock: a wait is written down, no time passes
    context.model = Recording(model)


@then("the client waited {seconds:d} seconds, once")
def client_waited(context, seconds):
    assert context.waits == [seconds], context.waits


@then("the client did not wait")
def client_did_not_wait(context):
    assert context.waits == [], context.waits


def ask_guarded(context, user, question):
    from guards import guarded_answer

    context.answer, context.verdict = run_captured(
        context, guarded_answer, question, user_id=user, model=context.model, engine=context.engine,
        data_version=context.version)


@when('customer "{user}" asks through the guardrails "{question}"')
def asks_through_guardrails(context, user, question):
    ask_guarded(context, user, question)


@when('customer "{user}" asks through the guardrails a question of {n:d} characters')
def asks_long_question(context, user, n):
    question = ("How many orders are there in the shop " * 20)[:n]
    assert len(question) == n
    ask_guarded(context, user, question)


@when("redteam.py runs with the stub LLM")
def redteam_runs(context):
    import redteam

    patch(context, redteam, "OUTPUT_DIR", context.tmp)          # the log goes to the scenario's temporary folder
    patch(context, redteam, "ensure_data", lambda: context.paths)
    run_captured(context, redteam.main, ["--model", "stub"])


@then('the check that stopped it is "{stage}"')
def check_that_stopped(context, stage):
    assert (context.verdict.stage or "none") == stage, (context.verdict.stage, context.verdict.reason)


@then('the rows are what the same query returns over only the orders of customer "{user}"')
def rows_are_own_rows(context, user):
    # A DuckDB connection of the scenario's own over the Parquet files, with the customer's filter written here:
    # the expected rows do not pass through the recipe's engine.
    con = duckdb.connect()
    con.execute(f"CREATE VIEW stores AS SELECT * FROM read_parquet('{context.paths['stores']}')")
    con.execute(f"CREATE VIEW orders AS SELECT * FROM read_parquet('{context.paths['orders']}') "
                f"WHERE customer_id = '{user}'")
    expected = [[float(v) if hasattr(v, "as_tuple") else (v if isinstance(v, (int, float, str)) or v is None
                 else str(v)) for v in row] for row in con.execute(context.answer.sql).fetchall()]
    assert context.answer.rows == expected, (context.answer.rows, expected)
    context.own_user = user


@then("no row of another customer's orders is in the rows")
def no_other_customers_rows(context):
    # Every order id in the rows belongs to the asking customer, and no other customer's id is in them.
    con = duckdb.connect()
    con.execute(f"CREATE VIEW orders AS SELECT * FROM read_parquet('{context.paths['orders']}')")
    theirs = {r[0] for r in con.execute(
        "SELECT order_id FROM orders WHERE customer_id <> ?", [context.own_user]).fetchall()}
    columns = context.answer.columns
    for row in context.answer.rows:
        named = dict(zip(columns, row))
        assert named.get("customer_id", context.own_user) in (context.own_user, None), row
        assert named.get("order_id") not in theirs, row


@then("the second value in the rows is {n:d}")
def second_value(context, n):
    assert context.answer.rows[0][1] == n, context.answer.rows


@then('the reason holds "{text}"')
def reason_holds(context, text):
    assert text in context.verdict.reason, context.verdict.reason


def red_team_log(context) -> list[dict]:
    return [json.loads(line) for line in (context.tmp / "redteam-log-stub.jsonl").read_text().splitlines()]


@then("the red-team log has one line for every case of attacks.toml and normal.toml")
def log_has_every_case(context):
    from redteam import load_cases

    assert [r["id"] for r in red_team_log(context)] == [c["id"] for c in load_cases()]


@then('the red-team log line of the case "{case}" has the outcome "{outcome}"')
def log_line_outcome(context, case, outcome):
    [row] = [r for r in red_team_log(context) if r["id"] == case]
    assert row["outcome"] == outcome, row


# ---- spans, metrics, alarms, analysis ----

def local_spans(context) -> list[dict]:
    return [json.loads(line) for line in (context.tmp / "spans.jsonl").read_text().splitlines()]


@when("the spans of that answer are written to a local file")
def spans_to_file(context):
    from spans import LocalSpans, spans_of

    LocalSpans(context.tmp / "spans.jsonl").put_all(spans_of(context.answer))


@given('the spans table of a run with "--target aws"')
def spans_table(context):
    in_moto(context)
    s = Settings()
    ensure_table(s.spans_table, s.region, key="trace_id", sort_key="span_id")


def spans_to_dynamo(context, name):
    from spans import DynamoSpans, spans_of

    in_moto(context)
    context.span_sink = DynamoSpans(name, Settings().region)
    run_captured(context, context.span_sink.put_all, spans_of(context.answer))


@when('the spans of that answer are written to the DynamoDB table "{name}"')
def spans_to_table(context, name):
    spans_to_dynamo(context, name)


@when('the spans of that answer are written to the DynamoDB table "{name}", which does not exist')
def spans_to_missing_table(context, name):
    spans_to_dynamo(context, name)


@when("the metrics of that answer are sent to CloudWatch")
def metrics_sent(context):
    from alarms import put_request_metrics

    in_moto(context)
    s = Settings()
    run_captured(context, put_request_metrics, context.answer.to_dict(), s.metric_namespace, s.region)


@when("the metrics of that answer are sent to a CloudWatch client that refuses the call")
def metrics_refused(context):
    from alarms import put_request_metrics

    class Refuses:
        def put_metric_data(self, **kwargs):
            raise ClientError({"Error": {"Code": "AccessDenied", "Message": "not allowed"}}, "PutMetricData")

    s = Settings()
    run_captured(context, put_request_metrics, context.answer.to_dict(), s.metric_namespace, s.region,
                 client=Refuses())


@when('the alarms are created with the email address "{email}"')
def alarms_created(context, email):
    from alarms import ensure_alarms

    in_moto(context)
    s = Settings()
    ensure_alarms(s.alarm_prefix, s.metric_namespace, s.topic_name, s.region, email=email)


@when("analyze.py reads that file")
def analyze_reads(context):
    import analyze

    patch(context, analyze, "OUTPUT_DIR", context.tmp)
    run_captured(context, analyze.main, [])


@then('the span ids are "{ids}"')
def span_ids(context, ids):
    assert [s["span_id"] for s in local_spans(context)] == ids.split(", ")


@then('the span kinds are "{kinds}"')
def span_kinds(context, kinds):
    assert [s["kind"] for s in local_spans(context)] == kinds.split(", ")


@then('every span after the first has parent_id "{parent}"')
def span_parents(context, parent):
    spans = local_spans(context)
    assert spans[0]["parent_id"] == "" and all(s["parent_id"] == parent for s in spans[1:])
    assert {s["trace_id"] for s in spans} == {context.answer.request_id}


@then('one Query by the request id returns the span ids "{ids}"')
def query_returns_spans(context, ids):
    items = context.span_sink.trace(context.answer.request_id)
    assert [i["span_id"] for i in items] == ids.split(", "), [i["span_id"] for i in items]


@then('the CloudWatch namespace "{namespace}" has the metrics "{names}"')
def namespace_has_metrics(context, namespace, names):
    cw = boto3.client("cloudwatch", region_name="ca-central-1")
    found = sorted(m["MetricName"] for m in cw.list_metrics(Namespace=namespace)["Metrics"])
    assert found == names.split(", "), found


def alarms_by_name() -> dict:
    cw = boto3.client("cloudwatch", region_name="ca-central-1")
    return {a["AlarmName"]: a for a in cw.describe_alarms()["MetricAlarms"]}


@then('the alarms of the account are "{names}"')
def alarms_are(context, names):
    assert sorted(alarms_by_name()) == names.split(", "), sorted(alarms_by_name())


@then('every alarm notifies the SNS topic "{topic}"')
def alarms_notify(context, topic):
    for alarm in alarms_by_name().values():
        assert [arn.split(":")[-1] for arn in alarm["AlarmActions"]] == [topic], alarm["AlarmActions"]


@then('every alarm compares with "{operator}" and treats missing data as "{missing}"')
def alarms_compare(context, operator, missing):
    for alarm in alarms_by_name().values():
        assert (alarm["ComparisonOperator"], alarm["TreatMissingData"]) == (operator, missing), alarm


@then("the alarms have these settings:")
def alarms_settings(context):
    alarms = alarms_by_name()
    for row in context.table:
        a = alarms[row["alarm"]]
        found = (a["MetricName"], a.get("Statistic") or a.get("ExtendedStatistic"), float(a["Threshold"]),
                 a["Period"], a["EvaluationPeriods"])
        wanted = (row["metric"], row["statistic"], float(row["threshold"]), int(row["period"]), int(row["periods"]))
        assert found == wanted, (row["alarm"], found, wanted)


@then('the table "calls per request" counts {a:d} request with {x:d} call and {b:d} request with {y:d} calls')
def calls_per_request(context, a, x, b, y):
    from analyze import QUERIES, load

    con = duckdb.connect()
    load(con, None, context.tmp / "spans.jsonl")
    assert dict(con.execute(QUERIES["calls per request"]).fetchall()) == {x: a, y: b}

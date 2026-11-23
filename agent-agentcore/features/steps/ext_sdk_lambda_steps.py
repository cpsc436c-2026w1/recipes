"""Steps of ext_sdk_lambda.feature. They call the extension's own code; the stand-ins are in tests/sdk_lambda_fakes.py.

`the status is` and `the reply is` are the recipe's steps (steps.py): they read context.report, which these steps
set to the answer of the function.
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import time
from contextlib import redirect_stdout
from pathlib import Path

from behave import given, then, when

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tests.sdk_lambda_fakes import (RefusingGateway, calls_of, cancelled_of, ext_env, load, scripted,  # noqa: E402
                                    started_of, trips_file)

WHOLE_DAY = [("sql", "SELECT COUNT(*) AS trips FROM trips"),
             ("output", {"reply": "There were 19074 trips.", "value": 19074})]


def _ext(context):
    if not getattr(context, "ext_ready", False):
        ext_env()
        context.ext_ready = True
        context.sdk = "strands"
        context.script = []
        context.scale = 1.0
        context.answers = {}
    return load("settings").load_settings()


def _engine(context):
    return load("engine").Engine(context.trips)


def _step(row) -> tuple:
    kind, content = row["reply"], row["content"]
    takes = float(row["takes"].rstrip(" s")) if "takes" in row.headings and row["takes"] else 0.0
    if content.startswith("run_sql: "):
        return ("sql", content[len("run_sql: "):], takes)
    try:
        return ("output", json.loads(content), takes)
    except ValueError:
        return ("prose", content, takes)


def _ask(context, question: str, sdk: str, s=None, model=None):
    s = s or _ext(context)
    if context.scale != 1.0:
        s = s.model_copy(update={"deadline_s": s.deadline_s * context.scale})
    handler = load("handler", "sdk_handler")
    model = model if model is not None else scripted(sdk, context.script, context.scale)
    start = time.monotonic()
    answer = asyncio.run(handler.answer_async(question, s, sdk=sdk, model=model, eng=_engine(context)))
    answer["wall"] = time.monotonic() - start
    answer["model"] = model
    return answer


def _num(text: str):
    return None if text == "" else int(text) if text.lstrip("-").isdigit() else float(text) if text[:1].isdigit() else text


# ---- data and model ---------------------------------------------------------------------------------------------
@given('the function ships the simulated data set SIM01 "three-zone day"')
def step_sim01(context):
    _ext(context)
    context.trips = trips_file(context.tmp)


@given('AGENT_SDK is "{sdk}"')
def step_sdk(context, sdk):
    _ext(context)
    os.environ["AGENT_SDK"] = sdk
    context.sdk = sdk


@given("the model's replies are:")
def step_replies(context):
    _ext(context)
    context.script = [_step(row) for row in context.table]


@given('the model\'s replies of the example "Trips of the whole day"')
def step_replies_whole_day(context):
    _ext(context)
    context.script = list(WHOLE_DAY)


@given('the model replies with a tool call run_sql "{sql}" every time')
def step_runaway(context, sql):
    _ext(context)
    context.script = [("sql", sql)]


@given("the clock runs {k:d} times faster than the wall clock")
def step_scale(context, k):
    _ext(context)
    context.scale = 1 / k


# ---- asking -------------------------------------------------------------------------------------------------------
@when('the analyst sends "{question}" to the function')
def step_send(context, question):
    """Through handler.handler, with a function URL event: the path Lambda takes."""
    s = _ext(context)
    handler = load("handler", "sdk_handler")
    handler._CACHE.update(settings=s, engine=_engine(context))
    model_for, handler.model_for = handler.model_for, lambda s_, sdk: scripted(sdk, context.script)
    try:
        out = handler.handler({"body": json.dumps({"question": question}), "isBase64Encoded": False}, None)
    finally:
        handler.model_for = model_for
        handler._CACHE.clear()
    assert out["statusCode"] == 200, out
    context.report = json.loads(out["body"])


@when('the analyst sends "{question}" with each AGENT_SDK:')
def step_send_each(context, question):
    context.rows = list(context.table)
    context.answers = {row["AGENT_SDK"]: _ask(context, question, row["AGENT_SDK"]) for row in context.rows}


@then("the value is {value:d}")
def step_value(context, value):
    assert context.report["value"] == value, context.report


@then('the SQL is "{sql}"')
def step_sql(context, sql):
    assert context.report["sql"] == sql, context.report


@then('the rows are "{text}"')
def step_rows(context, text):
    shown = "; ".join(", ".join(f"{k}: {v}" for k, v in row.items()) for row in context.report["rows"])
    assert shown == text, shown


@then('the reason is "{reason}"')
def step_reason(context, reason):
    assert context.report["reason"] == reason, context.report


@then("there is no value")
def step_no_value(context):
    assert context.report["value"] is None, context.report


@then("each answer has the {what} in its row")
def step_each_row(context, what):
    for row in context.rows:
        answer = context.answers[row["AGENT_SDK"]]
        for column in row.headings[1:]:
            assert answer[column] == _num(row[column]), (row["AGENT_SDK"], column, answer[column], row[column])


@then("the pydantic-ai answer validates as TripAnswer")
def step_validates(context):
    a = context.answers["pydantic-ai"]
    load("trip_contracts").TripAnswer.model_validate({"reply": a["reply"], "value": a["value"]})


@then('the SQL of each answer is "{sql}"')
def step_sql_each(context, sql):
    assert {a["sql"] for a in context.answers.values()} == {sql}, [a["sql"] for a in context.answers.values()]


@then("no answer has a value")
def step_no_values(context):
    assert all(a["value"] is None for a in context.answers.values()), context.answers


@then("the third model call, started at {start:d} s, was cancelled at {end:d} s")
def step_third_cancelled(context, start, end):
    for sdk, a in context.answers.items():
        model, k = a["model"], context.scale
        third = (started_of(model)[2] - started_of(model)[0]) / k
        assert abs(third - start) < 0.5, (sdk, third)
        assert cancelled_of(model) == 1 and calls_of(model) == 3, sdk
        assert abs(a["seconds"] / k - end) < 0.5, (sdk, a["seconds"] / k)


@then("each question ended within {limit:d} s")
def step_within(context, limit):
    for sdk, a in context.answers.items():
        assert a["wall"] / context.scale < limit, (sdk, a["wall"] / context.scale)


# ---- the tool -----------------------------------------------------------------------------------------------------
@when("the agent calls run_sql with each of these statements:")
def step_tool_calls(context):
    toollog = load("toollog")
    context.engine = _engine(context)
    tool = toollog.SqlTool(context.engine, toollog.Deadline(25))
    context.results = [tool(row["sql"]) for row in context.table]


@then("each of the {n:d} tool results is a rejection")
def step_rejections(context, n):
    assert len(context.results) == n and all(r.get("error", "").startswith("rejected") for r in context.results), \
        context.results


@then('the tool result\'s rows are "{cells}"')
def step_tool_rows(context, cells):
    assert [r.get("rows") for r in context.results] == [[[int(c)] for c in cells.split(",")]], context.results


@then("DuckDB ran {n:d} statements")
def step_ran(context, n):
    assert context.engine.statements_run == n


@then("table trips still has {n:d} rows")
def step_rows_left(context, n):
    assert context.engine.count() == n


# ---- refusals -----------------------------------------------------------------------------------------------------
@given("a stand-in server that answers the first model call like the course LLM gateway")
def step_refusing(context):
    _ext(context)
    context.gateway = RefusingGateway()
    context.closers.append(context.gateway.close)


@when("it answers with each of these:")
def step_refusals(context):
    s = _ext(context).model_copy(update={"course_gateway_url": context.gateway.url})
    handler = load("handler", "sdk_handler")
    context.refusals = []
    for row in context.table:
        context.gateway.requests.clear()
        context.gateway.answer = (int(row["http_status"]), row["code"], row["retry_after"] or None)
        answer = asyncio.run(handler.answer_async("How many trips?", s, sdk=row["AGENT_SDK"], eng=_engine(context)))
        context.refusals.append((row, answer, len(context.gateway.requests)))


@then('each answer has status "{status}", no value, and the status, code and retry_after of its row')
def step_refusal_rows(context, status):
    for row, a, _ in context.refusals:
        want = (status, None, int(row["http_status"]), row["code"], _num(row["retry_after"]))
        assert (a["status"], a["value"], a["http_status"], a["code"], a["retry_after"]) == want, (row.cells, a)


@then("the stand-in server received {n:d} request for each question")
def step_one_request(context, n):
    assert [r for _, _, r in context.refusals] == [n] * len(context.refusals)


# ---- deploy and clean-up on stand-ins -----------------------------------------------------------------------------
@given("a stand-in for the team sandbox with the package built")
def step_sandbox(context):
    from moto import mock_aws

    _ext(context)
    os.environ["LEDGER"] = str(context.tmp / "ledger.jsonl")
    os.environ["OWNER"] = "tester"
    mock = mock_aws()
    mock.start()
    context.closers.append(mock.stop)
    deploy = load("deploy", "sdk_deploy")
    site = context.tmp / "site"
    site.mkdir()
    (site / "placeholder.py").write_text("")
    context.zip = context.tmp / "agent-sdk.zip"
    deploy.build_zip(site, trips_file(context.tmp), context.zip)


@when("ext-sdk-lambda/deploy.py up runs")
@given("ext-sdk-lambda/deploy.py up has run")
def step_deploy(context):
    deploy = load("deploy", "sdk_deploy")
    context.url = deploy.up(_ext(context), context.zip)


@then('the function URL of "{name}" has auth type "{auth}"')
def step_url_auth(context, name, auth):
    import boto3

    c = boto3.client("lambda", region_name="ca-central-1").get_function_url_config(FunctionName=name)
    assert c["AuthType"] == auth and c["FunctionUrl"] == context.url, c


@then("the function is arm64, Python 3.12, 1024 MB, 60 s, with the token parameter's name and not the token")
def step_function_config(context):
    import boto3

    s = _ext(context)
    c = boto3.client("lambda", region_name="ca-central-1").get_function_configuration(FunctionName=s.function_name)
    env = c["Environment"]["Variables"]
    assert (c["Architectures"], c["Runtime"], c["MemorySize"], c["Timeout"]) == (["arm64"], "python3.12", 1024, 60), c
    assert env["KB_MCP_TOKEN_PARAM"] == s.token_param and "KB_MCP_TOKEN" not in env
    assert s.kb_mcp_token not in json.dumps(env)


@then("the function, the role, the log group and the parameter carry the tags Project, Team and Owner")
def step_tags(context):
    import boto3

    s, r = _ext(context), "ca-central-1"
    lam = boto3.client("lambda", region_name=r)
    found = {
        "function": lam.list_tags(Resource=lam.get_function(FunctionName=s.function_name)["Configuration"]
                                  ["FunctionArn"])["Tags"],
        "role": {t["Key"]: t["Value"] for t in boto3.client("iam").list_role_tags(RoleName=s.role_name)["Tags"]},
        "log group": boto3.client("logs", region_name=r).list_tags_log_group(logGroupName=s.log_group)["tags"],
        "parameter": {t["Key"]: t["Value"] for t in boto3.client("ssm", region_name=r).list_tags_for_resource(
            ResourceType="Parameter", ResourceId=s.token_param)["TagList"]},
    }
    for what, tag_map in found.items():
        assert {k: tag_map.get(k) for k in ("Project", "Team", "Owner")} == \
            {"Project": "436c", "Team": "test", "Owner": "tester"}, (what, tag_map)


@then('the ledger records "{state}" for the function, its URL, the role, the log group and the parameter')
def step_ledger(context, state):
    rows = [json.loads(line) for line in Path(os.environ["LEDGER"]).read_text().splitlines()]
    types = {r["type"] for r in rows if r["state"] == state}
    assert {"lambda-function", "lambda-function-url", "iam-role", "log-group", "ssm-parameter"} <= types, types


@when("ext-sdk-lambda/clean_up.py --yes runs")
def step_clean(context):
    clean = load("clean_up", "sdk_clean_up")
    context.clean = clean.clean(_ext(context), yes=True)


@then('no function, role, log group or parameter of "{name}" is left')
def step_nothing_left(context, name):
    import boto3

    r = "ca-central-1"
    assert boto3.client("lambda", region_name=r).list_functions()["Functions"] == []
    assert [x for x in boto3.client("iam").list_roles()["Roles"] if x["RoleName"].startswith(name)] == []
    assert boto3.client("logs", region_name=r).describe_log_groups(logGroupNamePrefix=f"/aws/lambda/{name}")[
        "logGroups"] == []
    assert boto3.client("ssm", region_name=r).describe_parameters()["Parameters"] == []
    assert context.clean.failed == [], context.clean.failed


@then('the lake bucket is still there, without objects under "{prefix}"')
def step_bucket_kept(context, prefix):
    import boto3

    lake = load("deploy", "sdk_deploy").lake
    s3 = boto3.client("s3", region_name="ca-central-1")
    bucket = lake.bucket_name()
    s3.head_bucket(Bucket=bucket)
    assert s3.list_objects_v2(Bucket=bucket, Prefix=prefix).get("KeyCount", 0) == 0


@then('a second clean-up reports every resource "{word}" and exits with code {code:d}')
def step_second_clean(context, word, code):
    clean = load("clean_up", "sdk_clean_up")
    st = clean.clean(_ext(context), yes=True)
    out = io.StringIO()
    with redirect_stdout(out):
        exit_code = st.report()
    lines = out.getvalue().splitlines()
    assert exit_code == code and lines and all(line.startswith(word) for line in lines), lines


# ---- @live: the deployed function (LIVE=1) ------------------------------------------------------------------------
@given('the function "<PREFIX>-agent-sdk" ships the simulated data set SIM01 "three-zone day"')
def step_live_function(context):
    context.invoke = load("invoke_url", "sdk_invoke_url")
    context.url = context.invoke.function_url()


@when('invoke_url.py --measure sends the 12 questions of the eval "trip-questions" 3 times each, one at a time')
def step_live_measure(context):
    import boto3

    inv = context.invoke
    creds = boto3.session.Session(region_name="ca-central-1").get_credentials().get_frozen_credentials()
    inv.send(context.url, inv.questions()[0], creds)
    context.times = [inv.send(context.url, q, creds)[2] for _ in range(3) for q in inv.questions()]


@then("the 35th of the 36 response times, in order, is under {limit:d} s")
def step_live_p95(context, limit):
    assert len(context.times) == 36 and sorted(context.times)[34] < limit, sorted(context.times)


@given('the function URL of "<PREFIX>-agent-sdk" with auth type AWS_IAM')
def step_live_url(context):
    import boto3

    context.invoke = load("invoke_url", "sdk_invoke_url")
    name = f"{os.environ.get('PREFIX', 'recipe')}-agent-sdk"
    c = boto3.client("lambda", region_name="ca-central-1").get_function_url_config(FunctionName=name)
    assert c["AuthType"] == "AWS_IAM", c
    context.url, context.function = c["FunctionUrl"], name
    context.since = int(time.time() * 1000)


@when("these requests are sent:")
def step_live_requests(context):
    import boto3

    creds = boto3.session.Session(region_name="ca-central-1").get_credentials().get_frozen_credentials()
    context.rows = list(context.table)
    context.statuses = [context.invoke.send(context.url, "How many trips were requested on 2024-03-14?",
                                            None if row["signed by"] == "nobody" else creds)[0]
                        for row in context.rows]


@then("each request gets the status in its row")
def step_live_statuses(context):
    assert context.statuses == [int(r["http_status"]) for r in context.rows], context.statuses


@then("the function's log has {n:d} invocation")
def step_live_log(context, n):
    import boto3

    logs = boto3.client("logs", region_name="ca-central-1")
    for _ in range(12):
        events = logs.filter_log_events(logGroupName=f"/aws/lambda/{context.function}", startTime=context.since,
                                        filterPattern='"START RequestId"').get("events", [])
        if len(events) >= n:
            break
        time.sleep(5)
    assert len(events) == n, len(events)


@given('the 12 cases of the eval "trip-questions"')
def step_live_cases(context):
    context.trip_eval = load("trip_eval", "sdk_trip_eval")
    assert len(context.trip_eval.load_cases()) == 12


@when("the model that the system uses answers each case 3 times with each AGENT_SDK")
def step_live_eval(context):
    te, s = context.trip_eval, load("settings").load_settings()
    trips = ROOT / "build" / "trips.parquet"          # SIM01 from DS01 (run.sh), never the synthetic stand-in
    assert trips.exists(), f"{trips} is missing: run ext-sdk-lambda/run.sh first"
    eng = load("engine").Engine(trips)
    cases = te.load_cases()
    refs = te.references(cases, eng)
    context.reports = {sdk: te.score(asyncio.run(te.run_sdk(sdk, cases, refs, s, eng, "gateway", 3)), refs)
                       for sdk in ("strands", "pydantic-ai")}


@then("at least {n:d} of the 36 runs are correct for each AGENT_SDK")
def step_live_runs(context, n):
    assert all(r["runs_correct"] >= n for r in context.reports.values()), context.reports


@then("at least {n:d} of the 12 cases pass for each AGENT_SDK")
def step_live_case_pass(context, n):
    assert all(r["cases_passed"] >= n for r in context.reports.values()), context.reports

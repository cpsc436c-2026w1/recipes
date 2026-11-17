"""Steps for every feature of this folder. Each step calls the recipe's own code; the stand-ins are in tests/."""
from __future__ import annotations

import io
import json
import os
import socket
import subprocess
import sys
import zipfile
from pathlib import Path

from behave import given, then, when

ROOT = Path(__file__).resolve().parents[2]


def settings():
    from main.config import load_settings

    return load_settings()


def names(csv: str) -> list[str]:
    return [part.strip() for part in csv.split(",") if part.strip()]


def library(context):
    if context.library is None:
        from main.store import MemoryStore
        from main.tools import LibraryTools

        context.library = LibraryTools(MemoryStore())
    return context.library


# ---- settings ------------------------------------------------------------------------------------------------
@given('the setting "{name}" is "{value}"')
def step_setting(context, name, value):
    os.environ[name] = value


@given('the setting "{name}" is empty')
def step_setting_empty(context, name):
    os.environ[name] = ""


@given('the setting "{name}" is not set')
def step_setting_missing(context, name):
    os.environ.pop(name, None)


@given('the older name "{name}" is "{value}"')
def step_old_name(context, name, value):
    os.environ[name] = value


@when("the settings are read")
def step_read_settings(context):
    try:
        context.settings = settings()
    except SystemExit as stop:
        context.error = str(stop)


@then('the settings object has "{field}" equal to "{value}"')
def step_settings_field(context, field, value):
    assert str(getattr(context.settings, field)) == value, getattr(context.settings, field)


@then("the settings object has a field for every line of .env.example and no other")
def step_settings_match(context):
    from main.config import ENV_NAMES, Settings
    from tests.offline_env import example_lines

    assert set(example_lines()) == set(ENV_NAMES.values()) and set(ENV_NAMES) == set(Settings.model_fields)


@then("a change to the settings object is refused")
def step_frozen(context):
    from pydantic import ValidationError

    try:
        context.settings.prefix = "other"
    except ValidationError:
        return
    raise AssertionError("the settings object took a change")


@then('every resource name is built from "{prefix}"')
def step_names(context, prefix):
    s = context.settings
    hyphen = [s.books_table, s.holds_table, s.tools_function, s.gateway_name, s.token_param, s.logfire_param,
              s.code_bucket("123456789012"), *(s.role_name(p) for p in ("lambda", "gateway", "runtime"))]
    assert all(n.startswith(f"{prefix}-agent-") for n in hyphen), hyphen
    assert s.runtime_name == prefix.replace("-", "_") + "_agent", s.runtime_name


# ---- one request on the stub model ---------------------------------------------------------------------------
@given('the stub model behaves "{behaviour}"')
def step_behaviour(context, behaviour):
    context.behaviour = behaviour


@given('the request id is "{request_id}"')
def step_request_id(context, request_id):
    context.request_id = request_id


@given("the request has already run longer than DEADLINE_S")
def step_late(context):
    context.late = True


@given("the tools are behind a stand-in for the Gateway")
def step_behind_gateway(context):
    from main.gateway_tools import GatewayLibrary
    from tests.fakes import FakeGatewayClient

    context.gateway_client = FakeGatewayClient(library(context))
    context.gateway = GatewayLibrary(context.gateway_client)


def build_agent(context, member):
    from main.agent import build
    from main.model import StubModel

    context.stub = StubModel(context.behaviour)
    gateway = getattr(context, "gateway", None)
    context.agent, context.run = build(settings(), context.stub, gateway or library(context), member,
                                       context.request_id, tools=gateway.tools if gateway else None)
    if context.late:
        context.run.cap.deadline -= settings().deadline_s + 1


@when('member "{member}" asks "{request}" and answers "{answer}"')
def step_ask_and_answer(context, member, request, answer):
    from main.agent import resume, start

    build_agent(context, member)
    context.report = start(context.agent, context.run, request)
    context.asked = []
    while context.report["status"] == "needs_approval":
        context.asked += context.report["approvals"]
        context.report = resume(context.agent, context.run, {a["id"]: answer for a in context.report["approvals"]})


@when('member "{member}" asks "{request}"')
def step_ask(context, member, request):
    from main.agent import start

    build_agent(context, member)
    context.report = start(context.agent, context.run, request)
    context.asked = list(context.report.get("approvals", []))


@when('the member answers "{answer}"')
def step_answer(context, answer):
    from main.agent import resume

    context.report = resume(context.agent, context.run, {a["id"]: answer for a in context.report["approvals"]})


@then('the status is "{status}"')
def step_status(context, status):
    assert context.report["status"] == status, context.report


@then("the report counts {model_calls:d} model calls and {tool_calls:d} tool calls")
def step_counts(context, model_calls, tool_calls):
    assert (context.report["model_calls"], context.report["tool_calls"]) == (model_calls, tool_calls), context.report


@then("the model was called {n:d} times")
def step_model_called(context, n):
    assert context.stub.calls == n, context.stub.calls


@then('the tools that ran are "{csv}"')
def step_tools_ran(context, csv):
    assert context.report["tools_ran"] == names(csv), context.report["tools_ran"]


@then('a tool call was refused with "{text}"')
def step_refused(context, text):
    assert any(text in r["refused"] for r in context.report["tools_refused"]), context.report["tools_refused"]


@then('the run was stopped by "{text}"')
def step_stopped(context, text):
    assert context.report["stopped"] == text, context.report["stopped"]


@then('the reply is "{text}"')
def step_reply(context, text):
    assert context.report["reply"] == text, context.report["reply"]


@then('the approval names "{title}" by "{author}" for member "{member}"')
def step_approval(context, title, author, member):
    approval = context.report["approvals"][0]
    assert (approval["title"], approval["author"], approval["member"]) == (title, author, member), approval


@then("{n:d} approvals were asked")
def step_approvals(context, n):
    assert len(context.asked) == n, context.asked


@then('member "{member}" has holds on "{csv}"')
def step_holds(context, member, csv):
    assert [h["book_id"] for h in library(context).get_holds(member)["holds"]] == names(csv)


@then('member "{member}" has no holds')
def step_no_holds(context, member):
    assert library(context).get_holds(member)["holds"] == []


@then('the store has one row, with the request key of "{request_id}", "{member}" and "{book_id}"')
def step_one_row(context, request_id, member, book_id):
    from main.request_key import request_key

    assert list(library(context).store.rows) == [(member, request_key(request_id, member, book_id))]


@then('the Gateway got the write with member "{member}" and a request key')
def step_gateway_write(context, member):
    writes = [args for name, args in context.gateway_client.calls if name == "place_hold"]
    assert len(writes) == 1 and writes[0]["member"] == member and len(writes[0]["request_id"]) == 16, writes


# ---- the tools and their contracts ---------------------------------------------------------------------------
@when("the tool \"{name}\" is called with '{arguments}'")
def step_call_tool(context, name, arguments):
    context.tool, context.result = name, library(context).call(name, json.loads(arguments))


@then('the result is an error that says "{text}"')
def step_error_result(context, text):
    assert text in context.result["error"], context.result


@then('the result says placed "{placed}" and duplicate_request "{duplicate}"')
def step_placed(context, placed, duplicate):
    assert (str(context.result["placed"]).lower(), str(context.result["duplicate_request"]).lower()) == (placed, duplicate)


@then("the result has the shape of the tool's result model")
def step_result_shape(context):
    from main.contracts import TOOLS, parse_result

    assert isinstance(parse_result(context.tool, context.result), TOOLS[context.tool][2])


@when("the tool definitions for the Gateway are built")
def step_schema(context):
    from main.contracts import tool_schema

    context.schema = tool_schema()


@then("each definition has the fields of the tool's pydantic model")
def step_schema_fields(context):
    from main.contracts import TOOLS

    assert [t["name"] for t in context.schema] == list(TOOLS)
    for t in context.schema:
        assert set(t["inputSchema"]["properties"]) == set(TOOLS[t["name"]][1].model_fields), t["name"]


@then("the definitions pass the parameter check of the AgentCore API")
def step_schema_valid(context):
    from botocore.validate import ParamValidator

    from tests.fakes import MODEL

    shape = MODEL.operation_model("CreateGatewayTarget").input_shape
    params = {"gatewayIdentifier": "test-agent-gateway-0000000001", "targetConfiguration": {"mcp": {"lambda": {
        "lambdaArn": "arn:aws:lambda:ca-central-1:123456789012:function:test-agent-tools",
        "toolSchema": {"inlinePayload": context.schema}}}}}
    report = ParamValidator().validate(params, shape)
    assert not report.has_errors(), report.generate_report()


# ---- deploy, Runtime sessions, clean-up ----------------------------------------------------------------------
@given("a stand-in for the team sandbox")
def step_sandbox(context):
    from moto import mock_aws

    import main.infra
    from main.infra import Clients
    from tests.fakes import FakeAgentCore

    mock = mock_aws()
    mock.start()
    context.closers.append(mock.stop)
    context.aws = Clients.for_region("ca-central-1")
    context.aws.agentcore = FakeAgentCore()
    saved = main.infra.BUILD
    main.infra.BUILD = context.tmp
    context.closers.append(lambda: setattr(main.infra, "BUILD", saved))
    context.lines = []


@given("the course token and the gateway address are set")
def step_token(context):
    os.environ["KB_MCP_TOKEN"] = "made-up-course-token"
    os.environ["COURSE_GATEWAY_URL"] = "https://llm.test.invalid/v1"


@when("the deploy runs")
def step_deploy(context):
    from main.infra import deploy
    from tests.offline_env import no_wait

    try:
        context.deployed = deploy(settings(), context.aws, log=context.lines.append, wait=no_wait,
                                  with_dependencies=False)
    except SystemExit as stop:
        context.error = str(stop)


@given("the recipe is deployed")
def step_deployed(context):
    step_sandbox(context)
    step_token(context)
    step_deploy(context)
    assert context.error is None, context.error


def agentcore_calls(context, operation):
    return [params for name, params in context.aws.agentcore.calls if name == operation]


@then('the run stops and names "{name}"')
def step_stops_naming(context, name):
    assert context.error and f"Set {name} in .env" in context.error, context.error


@then("no AWS call was made")
def step_no_aws_call(context):
    assert context.aws.agentcore.calls == [] and context.aws.dynamodb.list_tables()["TableNames"] == []


@then('there is a Gateway with protocol "{protocol}" and inbound authorization "{authorizer}"')
def step_gateway(context, protocol, authorizer):
    created = agentcore_calls(context, "CreateGateway")
    assert len(created) == 1 and (created[0]["protocolType"], created[0]["authorizerType"]) == (protocol, authorizer)


@then('its one target points at the tools function and lists the tools "{csv}"')
def step_target(context, csv):
    created = agentcore_calls(context, "CreateGatewayTarget")
    lam = created[0]["targetConfiguration"]["mcp"]["lambda"]
    assert len(created) == 1 and lam["lambdaArn"] == context.deployed["function_arn"]
    assert [t["name"] for t in lam["toolSchema"]["inlinePayload"]] == names(csv)


@when("the Gateway invokes the tools function for \"{tool}\" with '{arguments}'")
def step_invoke_function(context, tool, arguments):
    import importlib.util

    from main.infra import tools_zip
    from tests.fakes import lambda_context

    s = settings()
    folder = context.tmp / "function"
    if not folder.exists():
        zipfile.ZipFile(io.BytesIO(tools_zip())).extractall(folder)
        sys.path.insert(0, str(folder))
        context.closers.append(lambda: sys.path.remove(str(folder)))
        context.closers.append(lambda: [sys.modules.pop(n, None) for n in ("handler", "store", "tools")])
        os.environ.update(BOOKS_TABLE=s.books_table, HOLDS_TABLE=s.holds_table)
        spec = importlib.util.spec_from_file_location("handler", folder / "handler.py")
        context.handler = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(context.handler)
    context.result = context.handler.lambda_handler(json.loads(arguments), lambda_context(f"library-tools___{tool}"))


@then('the function returns the books "{csv}"')
def step_function_books(context, csv):
    assert [b["book_id"] for b in context.result["books"]] == names(csv), context.result


@then("the Runtime is created from the zip in the code bucket, with entry point agent_entry.py")
def step_runtime(context):
    from main.infra import CODE_KEY

    created = agentcore_calls(context, "CreateAgentRuntime")
    code = created[0]["agentRuntimeArtifact"]["codeConfiguration"]
    assert len(created) == 1 and code["entryPoint"] == ["agent_entry.py"] and code["code"]["s3"]["prefix"] == CODE_KEY
    body = context.aws.s3.get_object(Bucket=code["code"]["s3"]["bucket"], Key=CODE_KEY)["Body"].read()
    assert "main/agent.py" in zipfile.ZipFile(io.BytesIO(body)).namelist()


@then("the Runtime's configuration does not hold the course token")
def step_runtime_no_secret(context):
    created = agentcore_calls(context, "CreateAgentRuntime")[0]
    assert "made-up-course-token" not in json.dumps(created)
    assert created["environmentVariables"]["KB_MCP_TOKEN_PARAM"] == settings().token_param


@then("the course token is a SecureString parameter")
def step_secure_string(context):
    found = context.aws.ssm.describe_parameters()["Parameters"]
    assert [(p["Name"], p["Type"]) for p in found] == [(settings().token_param, "SecureString")]


@then("there is still one Gateway, one target and one Runtime")
def step_still_one(context):
    ac = context.aws.agentcore
    assert (len(ac.gateways), len(ac.targets), len(ac.runtimes)) == (1, 1, 1)
    assert len(agentcore_calls(context, "CreateGateway")) == len(agentcore_calls(context, "CreateAgentRuntime")) == 1


@given("a Runtime entry point on the stub model")
def step_sessions(context):
    from main.model import StubModel
    from main.session import Sessions

    context.sessions = Sessions(settings(), StubModel(context.behaviour), library(context))


@when("session \"{session}\" sends '{payload}'")
def step_session_payload(context, session, payload):
    context.report = context.sessions.handle(json.loads(payload), session)


@then('the error says "{text}"')
def step_error_says(context, text):
    assert text in context.report["error"], context.report


def left(context) -> dict:
    aws = context.aws
    return {"tables": aws.dynamodb.list_tables()["TableNames"],
            "functions": [f["FunctionName"] for f in aws.lambda_.list_functions()["Functions"]],
            "log groups": [g["logGroupName"] for g in aws.logs.describe_log_groups()["logGroups"]],
            "parameters": [p["Name"] for p in aws.ssm.describe_parameters()["Parameters"]],
            "buckets": [b["Name"] for b in aws.s3.list_buckets()["Buckets"]],
            "roles": [r["RoleName"] for r in aws.iam.list_roles()["Roles"]],
            "gateways": list(aws.agentcore.gateways), "targets": list(aws.agentcore.targets),
            "runtimes": list(aws.agentcore.runtimes)}


@given('the sandbox refuses "{operation}"')
def step_refuse(context, operation):
    context.aws.agentcore.refuse.add(operation)


@given('a role "{name}" that the recipe did not create is named in "{setting}"')
def step_own_role(context, name, setting):
    step_sandbox(context)
    step_token(context)
    trust = json.dumps({"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}]})
    os.environ[setting] = context.aws.iam.create_role(RoleName=name, AssumeRolePolicyDocument=trust)["Role"]["Arn"]


@when("clean_up.py runs")
def step_clean_up(context, dry_run=False):
    import clean_up
    from tests.offline_env import no_wait

    context.before = left(context)
    context.steps = clean_up.clean_up(settings(), context.aws, dry_run=dry_run, wait=no_wait)
    context.lines = []
    context.exit_code = context.steps.report(log=context.lines.append)


@when("clean_up.py runs with --dry-run")
def step_clean_up_dry(context):
    step_clean_up(context, dry_run=True)


@then("nothing the deploy created is left")
def step_nothing_left(context):
    assert all(context.before.values()), context.before
    assert not any(left(context).values()), left(context)


@then('the output has one "{word}" line for each resource')
def step_lines(context, word):
    with_word = [line for line in context.lines if line.startswith(word)]
    # the other lines are for what this deploy never made (no Logfire token, so no parameter for it)
    assert len(with_word) >= 12 and all(line.startswith((word, "absent")) for line in context.lines), context.lines


@then("everything the deploy created is still there")
def step_all_left(context):
    assert left(context) == context.before


@then("the exit code is {code:d}")
def step_exit_code(context, code):
    assert context.exit_code == code, context.exit_code


@then('the Runtime is still there and the other resources are gone, with one "{word}" line')
def step_failed_line(context, word):
    after = left(context)
    assert len(after["runtimes"]) == 1 and not any(v for k, v in after.items() if k not in ("runtimes", "log groups"))
    assert len([line for line in context.lines if line.startswith(word)]) == 1, context.lines


@then('the role "{name}" is the only role left')
def step_role_left(context, name):
    assert [r["RoleName"] for r in context.aws.iam.list_roles()["Roles"]] == [name]


# ---- the course LLM gateway ----------------------------------------------------------------------------------
@given("a local stand-in for the course LLM gateway")
def step_fake_gateway(context):
    from tests.fake_gateway import FakeGateway, tool_call

    context.fake = FakeGateway([tool_call("search_books", query="Emma"),
                                tool_call("place_hold", member="sam", book_id="b02"),
                                "I placed a hold on Emma for you."])
    context.closers.append(context.fake.close)
    os.environ.update(COURSE_GATEWAY_URL=context.fake.url, KB_MCP_TOKEN="made-up-course-token")


@given("the stand-in refuses every model call with status {status:d}")
def step_fake_refuses(context, status):
    context.fake.status = status


@then("the stand-in got {n:d} request")
def step_fake_count(context, n):
    assert len(context.fake.requests) == n, len(context.fake.requests)


@when('the command "{argv}" runs with no terminal to answer at')
def step_command_no_input(context, argv):
    step_command(context, argv)


@when('the command "{argv}" runs')
def step_command(context, argv):
    done = subprocess.run([sys.executable, "-m", "main.run", *argv.split()], cwd=ROOT, env=dict(os.environ),
                          stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)
    context.exit_code, context.printed = done.returncode, done.stdout + done.stderr


@then('the output has "{text}"')
def step_output(context, text):
    assert text in context.printed, context.printed


@then('the stand-in got {n:d} requests, each with the course token and the model "{model}"')
def step_fake_requests(context, n, model):
    got = context.fake.requests
    assert len(got) == n, len(got)
    assert all(r["authorization"] == "Bearer made-up-course-token" and r["body"]["model"] == model for r in got)


# ---- traces ----------------------------------------------------------------------------------------------------
@when("a request runs in a process of its own")
def step_probe(context):
    env = dict(os.environ)
    env.pop("LOGFIRE_SEND_TO_LOGFIRE", None)
    done = subprocess.run([sys.executable, str(ROOT / "tests" / "trace_probe.py")], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    context.seen = json.loads(done.stdout.strip().splitlines()[-1])


@then("no span was made and no tracer provider was set")
def step_no_spans(context):
    assert context.seen["spans"] == [] and context.seen["provider"] == "ProxyTracerProvider", context.seen


@then("no network connection was opened")
def step_no_connection(context):
    assert context.seen["connections"] == [], context.seen["connections"]


@then('there is one trace, under the span "{root}", with {chat:d} model spans and {tools:d} tool spans')
def step_one_trace(context, root, chat, tools):
    spans = context.seen["spans"]
    assert context.seen["traces"] == 1 and context.seen["roots"] == [root], context.seen
    assert spans.count("chat") == chat and len([n for n in spans if n.startswith("execute_tool")]) == tools, spans


@then("spans were made")
def step_spans(context):
    assert len(context.seen["spans"]) > 0


# ---- the eval --------------------------------------------------------------------------------------------------
@when("the Inspect task runs on the stub model")
def step_eval(context):
    from inspect_ai import eval as inspect_eval

    from evals.task import member_requests

    context.connections = []
    real = socket.socket.connect

    def record(sock, address):
        context.connections.append(address)
        raise OSError("offline")
    socket.socket.connect = record
    try:
        context.log = inspect_eval(member_requests(), log_dir=str(context.tmp / "logs"), display="none")[0]
    finally:
        socket.socket.connect = real


@then("every request has an outcome score computed from its tools and its holds")
def step_eval_scored(context):
    assert context.log.status == "success", context.log.error
    for sample in context.log.samples:
        score = sample.scores["outcome"]
        assert score.value == 1, (sample.id, score.explanation)
        assert "tools_ran" in score.metadata and "holds" in sample.metadata


@then("the accuracy is {value:g}")
def step_accuracy(context, value):
    metrics = {s.name: s.metrics for s in context.log.results.scores}
    assert metrics["outcome"]["accuracy"].value == value


@then("the eval opened no network connection")
def step_eval_offline(context):
    assert context.connections == [], context.connections

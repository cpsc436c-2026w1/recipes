"""Steps for the routed app: handling one request, the gateway stand-in, main/run.py."""

from __future__ import annotations

import json
import os
import shlex
import socket

import pytest
from behave import given, then, when
from support import GATEWAY_URL, TOKEN, bodies, gateway_handler, over_stand_in, patch, run_captured

import main.model as model_module
import main.run as run_module
from main.app import MisrouteLog, Models, handle
from main.config import Settings
from main.data import CATALOG, POLICY
from main.model import GatewayModel, _norm
from main.paths import STOCK_REPLY
from main.router import ROUTER_SYSTEM


def _log_lines(context) -> list[dict]:
    if not context.log_path.exists():
        return []
    return [json.loads(line) for line in context.log_path.read_text().splitlines() if line.strip()]


# ---- given ----

@given("the stub model with the labels of main/stub_replies.json")
def stub_model(context):
    context.models = run_module.make_models("stub", Settings())


@given('the stub router labels "{request}" as "{label}"')
def stub_labels(context, request, label):
    context.models.client.router[_norm(request)] = label


def _stand_in_models(context, **kw) -> None:
    client = over_stand_in(GatewayModel(GATEWAY_URL, TOKEN), gateway_handler(context.requests, **kw))
    s = Settings()
    context.models = Models(client, small=s.small_model, large=s.large_model)
    context.client = client


@given('a gateway stand-in whose router replies "{reply}"')
def stand_in_router(context, reply):
    _stand_in_models(context, router_reply=reply.strip())


@given('a gateway stand-in whose router replies "{reply}" and whose small model then replies with nothing')
def stand_in_empty_search(context, reply):
    def answer_by_model(request):
        body = json.loads(request.content)
        is_router = body["messages"][0]["content"] == ROUTER_SYSTEM
        text = reply if is_router else ("" if body["model"] == Settings().small_model else "A reply from the large model.")
        context.requests.append(request)
        return __import__("httpx").Response(200, json={
            "model": body["model"], "choices": [{"message": {"content": text}}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 100}})

    client = over_stand_in(GatewayModel(GATEWAY_URL, TOKEN), answer_by_model)
    s = Settings()
    context.models = Models(client, small=s.small_model, large=s.large_model)


@given("a gateway stand-in whose router replies with {n:d} characters")
def stand_in_long_reply(context, n):
    _stand_in_models(context, router_reply="x" * n)


@given("a gateway stand-in that names its models differently in its replies")
def stand_in_other_names(context):
    def handler(request):
        context.requests.append(request)
        body = json.loads(request.content)
        is_router = body["messages"][0]["content"] == ROUTER_SYSTEM
        return __import__("httpx").Response(200, json={
            "model": f"provider/{body['model'].upper()}-0613",
            "choices": [{"message": {"content": "hard" if is_router else "A reply."}}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 100}})

    client = over_stand_in(GatewayModel(GATEWAY_URL, TOKEN), handler)
    s = Settings()
    context.models = Models(client, small=s.small_model, large=s.large_model)


@given("GATEWAY_URL and COURSE_MCP_TOKEN are set to made-up values")
def gateway_env(context):
    os.environ["GATEWAY_URL"], os.environ["COURSE_MCP_TOKEN"] = GATEWAY_URL, TOKEN


@given("{name} is set to a made-up value and {missing} is not set")
def one_variable(context, name, missing):
    os.environ[name] = GATEWAY_URL if name == "GATEWAY_URL" else TOKEN
    os.environ.pop(missing, None)


@given('the variable {variable:w} names the model "{model}"')
def one_model(context, variable, model):
    assert variable in ("SMALL_MODEL", "LARGE_MODEL"), variable
    os.environ[variable] = model


@given('SMALL_MODEL is "{small}" and LARGE_MODEL is "{large}"')
def other_models(context, small, large):
    os.environ["SMALL_MODEL"], os.environ["LARGE_MODEL"] = small, large


def _gateway_is(context, **kw) -> None:
    """Every GatewayModel the recipe builds in this scenario gets the stand-in as its HTTP transport. The client
    is still built by the recipe from the environment: only what answers it is replaced."""
    built_init = GatewayModel.__init__
    handler = gateway_handler(context.requests, **kw)

    def init(self, *args, **kwargs):
        built_init(self, *args, **kwargs)
        over_stand_in(self, handler)
        context.client = self

    patch(context, model_module.GatewayModel, "__init__", init)


@given('the gateway is a stand-in whose router replies "{reply}"')
def gateway_is_router(context, reply):
    _gateway_is(context, router_reply=reply)


@given("the gateway is a stand-in that answers every call with HTTP {status:d}")
def gateway_is_status(context, status):
    _gateway_is(context, status=status)


@given('the gateway is a stand-in that answers HTTP {status:d} to requests about "{word}"')
def gateway_is_status_for(context, status, word):
    _gateway_is(context, status=status, only_about=word)


@given('the gateway is a stand-in whose router replies "{reply}" and whose large model answers HTTP {status:d}')
def gateway_router_and_failing_large(context, reply, status):
    _gateway_is(context, router_reply=reply, status=status, only_model=Settings().large_model)


@given('the gateway is a stand-in that answers HTTP {status:d} to the large model only')
def gateway_fails_large(context, status):
    _gateway_is(context, status=status, only_model=Settings().large_model)


@given("the gateway is a stand-in that answers every call with status 200 and {malformed}")
def gateway_malformed(context, malformed):
    _gateway_is(context, malformed=malformed)


@given("the gateway is a stand-in whose large model answers with status 200 and {malformed}")
def gateway_malformed_large(context, malformed):
    _gateway_is(context, malformed=malformed, only_model=Settings().large_model)


@then("the printed line of the request names 1 model call, the router's, and its cost")
def router_cost_kept(context):
    [row] = _json_lines(context)
    # 1000 input and 100 output tokens of the small model: 0.00006 CAD; the failed hard call adds nothing
    assert row["large_model_calls"] == 0 and float(row["cost_cad"]) == 0.00006, row


@then('the route log\'s line for the request has path "{path}" and error {flag}')
def route_log_error(context, path, flag):
    [line] = [json.loads(x) for x in context.routes_path.read_text().splitlines()]
    assert (line["path"], line["error"]) == (path, flag == "true") and line["cost_cad"] == 0.00006, line


@then("the misroute log has 1 line that says the request ended in error")
def misroute_error(context):
    [line] = _log_lines(context)
    assert line.get("error") is True and line["sent_to"] == "hard", line


@given("the gateway is a stand-in that does not answer within the time limit of one call")
def gateway_is_silent(context):
    _gateway_is(context, times_out=True)


@given("no course token and no gateway address in the environment")
def no_token(context):
    assert "COURSE_MCP_TOKEN" not in os.environ and "GATEWAY_URL" not in os.environ


@given("every network connection fails the scenario")
def no_network(context):
    def refuse(self, address):
        raise AssertionError(f"the run opened a network connection to {address}")

    patch(context, socket.socket, "connect", refuse)


# ---- when ----

@when('the app handles "{request}"')
def app_handles(context, request):
    context.log = MisrouteLog(context.log_path)
    context.out = run_captured(context, handle, request, context.models, context.log)
    assert context.error is None, context.printed_err


@when("main/run.py runs on the stub")
def run_stub(context):
    run_captured(context, run_module.main, ["--log", str(context.log_path), "--routes", str(context.routes_path)])


@when('main/run.py runs with "{args}"')
def run_with(context, args):
    run_captured(context, run_module.main, [*shlex.split(args), "--log", str(context.log_path), "--routes", str(context.routes_path)])


# ---- then: the result of one request ----

@then('the label is "{label}" and the path is "{path}"')
def label_and_path(context, label, path):
    assert (context.out["label"], context.out["path"]) == (label, path), context.out


@then('the path is "{path}"')
def path_is(context, path):
    assert context.out["path"] == path, context.out


@then('the reply is "{reply}"')
def reply_is(context, reply):
    assert context.out["reply"] == reply, context.out["reply"]


@then("the reply is the stock reply")
def reply_is_stock(context):
    assert context.out["reply"] == STOCK_REPLY


@then('the model calls are "{parts}"')
def model_calls_are(context, parts):
    assert [c["part"] for c in context.out["calls"]] == [p.strip() for p in parts.split(",")], context.out["calls"]


@then("the request made {n:d} large-model call")
@then("the request made {n:d} large-model calls")
def large_calls(context, n):
    assert context.out["large_model_calls"] == n, context.out


@then('the result has the fields "{fields}"')
def result_fields(context, fields):
    assert list(context.out) == [f.strip() for f in fields.split(",")], list(context.out)


@then('every model call in the result has "{fields}"')
def call_fields(context, fields):
    assert len(context.out["calls"]) == 2
    for call in context.out["calls"]:
        assert list(call) == [f.strip() for f in fields.split(",")], call


@then("the result's cost is the sum of its calls' costs")
def cost_is_sum(context):
    assert context.out["cost_cad"] > 0
    assert context.out["cost_cad"] == pytest.approx(sum(c["cost_cad"] for c in context.out["calls"]))


@then("the {part} call costs {cost:g} CAD for {tin:d} input and {tout:d} output tokens")
def call_costs(context, part, cost, tin, tout):
    call = next(c for c in context.out["calls"] if c["part"] == part)
    assert (call["tokens_in"], call["tokens_out"]) == (tin, tout), call
    assert call["cost_cad"] == pytest.approx(cost, rel=1e-9), call


# ---- then: the misroute log ----

@then("nothing is in the misroute log")
def log_empty(context):
    assert context.log.lines == [] and _log_lines(context) == []


@then("the misroute log has {n:d} line")
@then("the misroute log has {n:d} lines")
def log_has(context, n):
    assert len(context.log.lines) == n and len(_log_lines(context)) == n, _log_lines(context)


@then("the misroute log file has {n:d} lines")
def log_file_has(context, n):
    assert len(_log_lines(context)) == n, _log_lines(context)


@then('the last misroute line has reason "{reason}" and was sent to "{path}"')
def last_line(context, reason, path):
    line = _log_lines(context)[-1]
    assert (line["reason"], line["sent_to"]) == (reason, path), line
    assert line["request"] == context.out["request"] and line["request_id"] == context.out["request_id"] and line["ts"]


@then('the last misroute line has the router\'s reply "{reply}"')
def last_line_reply(context, reply):
    line = _log_lines(context)[-1]
    assert line["router_reply"] == reply and line["label"] is None, line


@then("the router's reply in the last misroute line has {n:d} characters")
def last_line_reply_length(context, n):
    assert len(_log_lines(context)[-1]["router_reply"]) == n


# ---- then: what the gateway stand-in received ----

@then("the gateway received {n:d} request")
@then("the gateway received {n:d} requests")
def gateway_received(context, n):
    assert len(context.requests) == n, len(context.requests)


@then('gateway request {i:d} asks the model "{model}" with max_tokens {n:d}')
def request_asks(context, i, model, n):
    body = bodies(context.requests)[i - 1]
    assert (body["model"], body["max_tokens"]) == (model, n), body


@then("gateway request {i:d} has the label definitions as its system prompt")
def request_system_is_labels(context, i):
    assert bodies(context.requests)[i - 1]["messages"][0] == {"role": "system", "content": ROUTER_SYSTEM}


@then('gateway request {i:d} has "{text}" as its user message')
def request_user_is(context, i, text):
    assert bodies(context.requests)[i - 1]["messages"][1] == {"role": "user", "content": text}


@then("gateway request {i:d} lists {n:d} products in its user message")
def request_lists(context, i, n):
    user = bodies(context.requests)[i - 1]["messages"][1]["content"]
    listed = [line for line in user.splitlines() if line.startswith("- ")]
    assert len(listed) == n, user
    assert all(any(p["name"] in line for p in CATALOG) for line in listed)


@then("gateway request {i:d} has the catalog and the policy in its system prompt")
def request_has_catalog(context, i):
    system = bodies(context.requests)[i - 1]["messages"][0]["content"]
    assert json.dumps(CATALOG) in system and POLICY in system


@then('every gateway request went to "{url}"')
def requests_went_to(context, url):
    assert {str(r.url) for r in context.requests} == {url}


@then("every gateway request carries the course token as the bearer key")
def requests_carry_token(context):
    assert {r.headers["authorization"] for r in context.requests} == {f"Bearer {TOKEN}"}


@then("every gateway request has temperature 0")
def requests_temperature(context):
    assert {b["temperature"] for b in bodies(context.requests)} == {0}


@then("the time limit the recipe set for one call is {seconds:g} seconds")
def time_limit(context, seconds):
    t = context.client.http.timeout
    assert (t.connect, t.read, t.write, t.pool) == (seconds,) * 4, t


# ---- then: what a run printed ----

@then("the exit code is {code:d}")
def exit_code_is(context, code):
    assert context.exit_code == code, (context.exit_code, context.printed_err[-500:])


def _json_lines(context) -> list[dict]:
    return [json.loads(line) for line in context.printed.splitlines() if line.startswith("{")]


@then('{n:d} printed line is JSON with path "{path}" and an error that names "{text}"')
@then('{n:d} printed lines are JSON with path "{path}" and an error that names "{text}"')
def lines_in_error(context, n, path, text):
    rows = [r for r in _json_lines(context) if r["path"] == path and text in r.get("error", "")]
    assert len(rows) == n and all(r["reply"] == "" for r in rows), context.printed


@then('{n:d} printed lines are JSON with path "{path}" and no error')
def lines_without_error(context, n, path):
    assert sum(r["path"] == path and "error" not in r for r in _json_lines(context)) == n, context.printed


@then('the error output holds "{text}"')
def error_output_holds_one(context, text):
    assert text in context.printed_err, context.printed_err


@then("the result's error is empty")
def result_error_empty(context):
    assert context.out["error"] == ""


@then('the printed output holds "{text}"')
def printed_holds(context, text):
    assert text in context.printed, context.printed[-800:]


@then('{n:d} printed lines are JSON with "{fields}"')
def printed_json(context, n, fields):
    rows = [json.loads(line) for line in context.printed.splitlines() if line.startswith("{")]
    assert len(rows) == n and all(list(r) == [f.strip() for f in fields.split(",")] for r in rows), rows


@then('{n:d} printed lines start with "{text}"')
def printed_lines_start(context, n, text):
    assert sum(line.startswith(text) for line in context.printed.splitlines()) == n, context.printed


@then('the last printed line starts with "{text}"')
def last_line_starts(context, text):
    assert context.printed.splitlines()[-1].startswith(text), context.printed.splitlines()[-1]


@then('the last printed line ends with "{text}" and the path of the log')
def last_line_ends(context, text):
    assert context.printed.splitlines()[-1].endswith(f"{text} {context.log_path.resolve()}")


@then("the course token is in no printed line and in no line of the misroute log")
def token_nowhere(context):
    assert context.log_path.exists(), "the run wrote no misroute log, so the check would pass on nothing"
    assert TOKEN not in context.printed + context.printed_err + context.log_path.read_text()


# ---- then: the route log ----

@then("the route log has {n:d} lines, one per request, each with \"{fields}\"")
def route_log_lines(context, n, fields):
    lines = [json.loads(x) for x in context.routes_path.read_text().splitlines()]
    assert len(lines) == n and len({x["request_id"] for x in lines}) == n, lines
    assert all(list(x) == [f.strip() for f in fields.split(",")] for x in lines), lines[0]


@then('the route log counts {n:d} requests on the path "{path}", {m:d} of them misrouted')
def route_log_paths(context, n, path, m):
    lines = [json.loads(x) for x in context.routes_path.read_text().splitlines() if json.loads(x)["path"] == path]
    assert (len(lines), sum(x["misrouted"] for x in lines)) == (n, m), lines


@then("no request's text is in the route log")
def route_log_no_text(context):
    text = context.routes_path.read_text()
    for request in (run_module.HERE / "requests.txt").read_text().splitlines():
        assert request.strip() and request not in text, request
    assert "reply" not in text and "request\"" not in text


@then("the route log's costs add up to the total the run printed")
def route_log_total(context):
    lines = [json.loads(x) for x in context.routes_path.read_text().splitlines()]
    printed = float(context.printed.splitlines()[-1].split("total ")[1].split(" CAD")[0])
    assert round(sum(x["cost_cad"] for x in lines), 6) == printed

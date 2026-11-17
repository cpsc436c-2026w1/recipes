"""Steps for the main scenario: asking a question, the LLM stand-ins, the gateway stand-in, main/run.py."""

from __future__ import annotations

import calendar
import json
import os
import socket
import time

import duckdb
from behave import given, then, when
from support import (GATEWAY_URL, FailsSomeQuestions, Recording, Scripted, gateway_stand_in, gateway_that_times_out,
                     gateway_that_cuts_its_tool_call, gateway_that_uses_its_cap, patch, run_captured, set_env)

from main.chat import answer
from main.config import ROOT, Settings
from main.engine import Engine
from main.model import GatewayModel, StubModel, _norm
from main.traces import LocalTraces

assert GATEWAY_URL  # named in main.feature


# ---- given: the tables and the LLM ----

@given("the shop's tables are loaded in DuckDB")
def tables_loaded(context):
    context.engine = Engine(context.paths)


@given("the engine stops a query after {seconds:g} seconds")
def engine_with_time_limit(context, seconds):
    context.engine = Engine(context.paths, query_timeout_s=seconds)


@given("the stub LLM with the fixed replies of main/stub_replies.json")
def stub_llm(context):
    context.model = Recording(StubModel.from_file(ROOT / "main" / "stub_replies.json"))


@given('the LLM answers "{question}" with the query "{sql}"')
def llm_writes_query(context, question, sql):
    context.model.inner.script[_norm(question)] = {"sql": sql}


@given('an LLM that calls the tool "{tool}" and then replies "{reply}"')
def llm_calls_other_tool(context, tool, reply):
    context.model = Recording(Scripted(("tool", tool, "SELECT 1"), ("reply", reply)))


@given("an LLM that asks for the tool on every call")
def llm_never_stops(context):
    context.model = Recording(Scripted(("tool", "run_sql", "SELECT COUNT(*) FROM orders")))


@given('an LLM that fails the question "{question}" with "{message}"')
def llm_fails_one_question(context, question, message):
    context.model = Recording(FailsSomeQuestions(context.model.inner, {question: message}))


@given("a gateway stand-in that answers every call with HTTP {status:d}")
def gateway_answers(context, status):
    context.gateway_requests = []
    context.model = Recording(gateway_stand_in(status, context.gateway_requests))


@given('a gateway stand-in whose reply has no text and finish_reason "length"')
def gateway_uses_its_cap(context):
    context.gateway_requests = []
    context.model = Recording(gateway_that_uses_its_cap(context.gateway_requests, query_first=False))


@given('a gateway stand-in that asks for a query, then replies with no text and finish_reason "length"')
def gateway_queries_then_uses_its_cap(context):
    context.gateway_requests = []
    context.model = Recording(gateway_that_uses_its_cap(context.gateway_requests, query_first=True))


@given('a gateway stand-in whose tool call is cut in its arguments, with finish_reason "length"')
def gateway_cuts_its_tool_call(context):
    context.gateway_requests = []
    context.model = Recording(gateway_that_cuts_its_tool_call(context.gateway_requests))


@given("a gateway stand-in that does not answer within the time limit of one call")
def gateway_times_out(context):
    context.gateway_requests = []
    context.model = Recording(gateway_that_times_out(context.gateway_requests))


@given('CHAT_MODEL is "{model}"')
def chat_model_is(context, model):
    set_env(context, "CHAT_MODEL", model)


@given("traces are written to a local file")
def local_traces(context):
    context.trace_path = context.tmp / "traces.jsonl"
    context.traces = LocalTraces(context.trace_path)


@given("no course token and no gateway address in the environment")
def no_token(context):
    assert "COURSE_MCP_TOKEN" not in os.environ and "GATEWAY_URL" not in os.environ


@given("every network connection fails the scenario")
def no_network(context):
    def refuse(self, address):
        raise AssertionError(f"the run opened a network connection to {address}")

    patch(context, socket.socket, "connect", refuse)


# ---- when ----

@when('customer "{user}" asks "{question}"')
def customer_asks(context, user, question):
    context.answer = run_captured(context, answer, question, user_id=user, model=context.model,
                                  engine=context.engine, data_version=context.version, traces=context.traces)


@when('the engine is asked, without the SQL check, to run "{sql}"')
def engine_runs_unchecked(context, sql):
    try:
        context.engine.run(sql)
        context.engine_error = None
    except duckdb.Error as e:
        context.engine_error = e


@when('main/run.py asks "{first}" and "{second}"')
def run_asks_two(context, first, second):
    import main.run as run

    traces = LocalTraces(context.tmp / "traces.jsonl")
    patch(context, run, "build", lambda target, kind, settings: (context.engine, context.model, traces,
                                                                 context.version))
    run_captured(context, run.main, ["--target", "local", "--model", "stub", "--question", first,
                                     "--question", second])


@when('main/run.py runs with "{args}"')
def run_with_args(context, args):
    import main.run as run

    assert "aws" not in args and "gateway" not in args, "scenarios run offline: --target local --model stub only"
    # The run reads the scenario's Parquet files and writes its traces to the scenario's temporary folder.
    patch(context, run, "ensure_data", lambda: context.paths)
    patch(context, run, "OUTPUT_DIR", context.tmp)
    context.trace_path = context.tmp / "traces.jsonl"
    run_captured(context, run.main, args.split())


@when("the gateway client is made without GATEWAY_URL and COURSE_MCP_TOKEN")
def gateway_client_without_settings(context):
    s = Settings()
    assert (s.gateway_url, s.token) == ("", "")
    run_captured(context, GatewayModel, s.gateway_url, s.token, s.chat_model)


# ---- then: the result ----

@then('the status is "{status}"')
def status_is(context, status):
    assert context.answer.status == status, (context.answer.status, context.answer.error)


@then("the question ends without an error")
def no_error(context):
    assert context.answer.status != "error" and context.answer.error == "", context.answer.error


@then('the reply is "{text}"')
def reply_is(context, text):
    assert context.answer.reply == text, context.answer.reply


@then("the reply is empty")
def reply_is_empty(context):
    assert context.answer.reply == "", context.answer.reply


@then('the last model call step has status "{status}"')
def last_model_step(context, status):
    step = [s for s in context.answer.steps if s.kind == "model_call"][-1]
    assert step.status == status, step


@then('the error holds "{text}"')
def error_holds(context, text):
    assert text in context.answer.error, context.answer.error


@then('the rows hold the value of the check query "{sql}"')
def rows_equal_check_query(context, sql):
    # An unlocked DuckDB connection of the scenario's own, over the same Parquet files: the expected value does
    # not pass through the recipe's engine, its SQL check or the LLM stand-in.
    con = duckdb.connect()
    for table, path in context.paths.items():
        con.execute(f"CREATE VIEW {table} AS SELECT * FROM read_parquet('{path}')")
    context.check_value = float(con.execute(sql).fetchone()[0])
    assert context.answer.rows and context.answer.rows[0][0] == context.check_value, (
        context.answer.rows, context.check_value)


@then("the reply holds that value")
def reply_holds_value(context):
    assert str(context.check_value) in context.answer.reply, context.answer.reply


@then("the rows are empty")
def rows_empty(context):
    assert context.answer.rows == [] and context.answer.sql == ""


@then('the SQL that ran starts with "{text}"')
def sql_starts_with(context, text):
    assert context.answer.sql.startswith(text), context.answer.sql


@then('the steps are "{kinds}"')
def steps_are(context, kinds):
    assert [s.kind for s in context.answer.steps] == kinds.split(", "), [s.kind for s in context.answer.steps]


@then("the result has {n:d} rows")
def result_has_rows(context, n):
    assert len(context.answer.rows) == n, len(context.answer.rows)


@then('the tool result the LLM received has {n:d} rows and says "{what}" were cut')
def tool_result_truncated(context, n, what):
    [result] = context.model.tool_results()
    assert len(result["rows"]) == n and result["truncated"] == what, (len(result["rows"]), result["truncated"])


@then('the tool result the LLM received says "{what}" was cut')
def tool_result_says(context, what):
    [result] = context.model.tool_results()
    assert result["truncated"] == what, result["truncated"]


@then("the first value in the rows has {n:d} characters")
def first_value_length(context, n):
    assert len(context.answer.rows[0][0]) == n, len(context.answer.rows[0][0])


def trace_tool_step(context) -> dict:
    """The tool call's step as the trace item holds it (main/traces.py)."""
    from main.traces import trace_item

    return next(s for s in trace_item(context.answer)["steps"] if s["kind"] == "tool_call")


@then('the tool call step in the trace says "{what}" were cut')
def trace_step_says_were(context, what):
    assert trace_tool_step(context)["truncated"] == what, trace_tool_step(context)


@then('the tool call step in the trace says "{what}" was cut')
def trace_step_says_was(context, what):
    assert trace_tool_step(context)["truncated"] == what, trace_tool_step(context)


@then("the tool call step in the trace says nothing was cut")
def trace_step_says_nothing(context):
    assert trace_tool_step(context)["truncated"] == "", trace_tool_step(context)


@then('the tool call has status "{status}"')
def tool_call_status(context, status):
    calls = [s for s in context.answer.steps if s.kind == "tool_call"]
    assert calls and calls[0].status == status, [(s.kind, s.status) for s in context.answer.steps]


@then('the tool result the LLM received starts with "{text}"')
def tool_result_starts_with(context, text):
    results = context.model.tool_results()
    assert results and results[0].get("error", "").startswith(text), results


@then("no SQL ran")
def no_sql_ran(context):
    ran = [s.detail for s in context.answer.steps if s.kind == "tool_call" and s.status == "ok"]
    assert ran == [] and context.answer.sql == "" and context.answer.rows == [], (ran, context.answer.sql)


@then('the table "{table}" still has {n:d} rows')
def table_still_has_rows(context, table, n):
    assert context.engine.con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == n


@then("{n:d} queries ran")
def queries_ran(context, n):
    assert sum(s.kind == "tool_call" for s in context.answer.steps) == n, [s.kind for s in context.answer.steps]


@then("the time limit of one model call is {seconds:d} seconds")
def model_call_time_limit(context, seconds):
    timeout = context.model.inner.http.timeout
    assert (timeout.connect, timeout.read, timeout.write, timeout.pool) == (seconds,) * 4, timeout


@then("the question took less than {seconds:d} seconds")
def question_took_less_than(context, seconds):
    assert context.answer.latency_ms < seconds * 1000, context.answer.latency_ms


@then("an engine that is given no time limit stops a query after {seconds:d} seconds")
def default_query_time_limit(context, seconds):
    assert Engine(context.paths).query_timeout_s == seconds
    assert context.engine.for_rows_of({"orders": "customer_id"}, "c-0007").query_timeout_s == seconds


@then("the LLM was called {n:d} times")
def llm_called(context, n):
    assert len(context.model.calls) == n, len(context.model.calls)


@then('DuckDB raises an error that holds "{words}"')
def duckdb_raises(context, words):
    assert context.engine_error is not None, "the engine ran the statement"
    assert words in str(context.engine_error), str(context.engine_error)


# ---- then: the request the gateway received ----

@then("the gateway stand-in received {n:d} request")
def gateway_received(context, n):
    assert len(context.gateway_requests) == n, len(context.gateway_requests)


@then("the gateway stand-in received {n:d} requests")
def gateway_received_several(context, n):
    assert len(context.gateway_requests) == n, len(context.gateway_requests)


@then('the request went to "{url}"')
def request_went_to(context, url):
    assert str(context.gateway_requests[0].url) == url, str(context.gateway_requests[0].url)


@then('the request\'s Authorization header is "{value}"')
def request_authorization(context, value):
    assert context.gateway_requests[0].headers.get("authorization") == value


@then('the request body field "{field}" is {value}')
def request_body_field(context, field, value):
    body = json.loads(context.gateway_requests[0].content)
    assert body[field] == json.loads(value), (field, body.get(field))


@then('every request body has the field "{field}" {value}')
def every_request_body_field(context, field, value):
    bodies = [json.loads(r.content) for r in context.gateway_requests]
    assert bodies and all(b[field] == json.loads(value) for b in bodies), [b.get(field) for b in bodies]


# ---- then: what was printed, and the exit code ----

def printed_lines(context) -> list[str]:
    return [line for line in context.printed.splitlines() if line.strip()]


@then("the exit code is {code:d}")
def exit_code_is(context, code):
    assert context.exit_code == code, (context.exit_code, context.printed_err)


@then('printed line {n:d} has status "{status}" and an "error" field that holds "{text}"')
def printed_line_error(context, n, status, text):
    line = json.loads(printed_lines(context)[n - 1])
    assert line["status"] == status and text in line["error"], line


@then('printed line {n:d} has status "{status}" and no "error" field')
def printed_line_no_error(context, n, status):
    line = json.loads(printed_lines(context)[n - 1])
    assert line["status"] == status and "error" not in line, line


@then("{n:d} questions are printed, each with a status")
def questions_printed(context, n):
    lines = [json.loads(line) for line in printed_lines(context)[:-1]]
    assert len(lines) == n and all(line["status"] for line in lines), lines


@then('the last printed line starts with "{text}"')
def last_line_starts_with(context, text):
    assert printed_lines(context)[-1].startswith(text), printed_lines(context)[-1]


@then("the last printed line gives, in CAD, the sum of the cost of the run's {n:d} trace items")
def last_line_total(context, n):
    items = trace_items(context)
    total = sum(item["cost_cad"] for item in items)
    assert len(items) == n and total > 0, (len(items), total)
    assert f"total {total:.6f} CAD" in printed_lines(context)[-1], (total, printed_lines(context)[-1])


@then("the last printed line names the file the traces are in")
def last_line_names_trace_file(context):
    assert printed_lines(context)[-1].endswith(f"traces in {context.trace_path}"), printed_lines(context)[-1]


@then('the last printed line is "{text}"')
def last_line_is(context, text):
    assert printed_lines(context)[-1] == text, printed_lines(context)[-1]


@then('the printed output holds "{text}"')
def printed_holds(context, text):
    assert text in context.printed, context.printed


@then('the error output holds "{text}"')
def error_output_holds(context, text):
    assert text in context.printed_err, context.printed_err


@then('{n:d} printed lines start with "{text}"')
def n_printed_lines_start(context, n, text):
    assert sum(line.startswith(text) for line in printed_lines(context)) == n, context.printed


@then('{n:d} error lines start with "{text}"')
def n_error_lines_start(context, n, text):
    assert sum(line.startswith(text) for line in context.printed_err.splitlines()) == n, context.printed_err


@then('"{secret}" is not in the result, the trace file or the printed output')
def secret_nowhere(context, secret):
    assert context.gateway_requests, "the stand-in received no request, so the token was never used"
    assert secret in context.gateway_requests[0].headers["authorization"]  # it was sent, and only there
    for name, text in (("result", json.dumps(context.answer.to_dict())),
                       ("trace file", context.trace_path.read_text()),
                       ("printed output", context.printed + context.printed_err)):
        assert secret not in text, f"the token is in the {name}"


# ---- then: the trace item of a local run ----

def trace_items(context) -> list[dict]:
    return [json.loads(line) for line in context.trace_path.read_text().splitlines() if line.strip()]


@then('the trace file has {n:d} item with status "{status}"')
def trace_file_has(context, n, status):
    items = trace_items(context)
    assert len(items) == n and all(i["status"] == status for i in items), [(i["status"]) for i in items]


@then("the trace item holds the question, the SQL and {n:d} steps")
def trace_item_holds(context, n):
    [item] = trace_items(context)
    assert item["question"] == context.answer.question and item["sql"] == context.answer.sql and item["sql"]
    assert len(item["steps"]) == n, item["steps"]


@then("the trace item holds {n:d} rows of the result")
def trace_item_rows(context, n):
    [item] = trace_items(context)
    assert item["rows"] == context.answer.rows[:n] and len(item["rows"]) == n, item["rows"]


@then('the trace item holds a {n:d}-character "{field}" and no "{absent}"')
def trace_item_hash(context, n, field, absent):
    [item] = trace_items(context)
    assert len(item[field]) == n and absent not in item, item.keys()
    assert context.answer.user_id not in json.dumps(item), "the user id is in the trace item"


@then("the trace item expires {days:d} days after it was written")
def trace_item_expires(context, days):
    [item] = trace_items(context)
    written = calendar.timegm(time.strptime(item["created_at"], "%Y-%m-%dT%H:%M:%SZ"))
    assert abs(item["expires_at"] - written - days * 86400) <= 2, (item["expires_at"], written)

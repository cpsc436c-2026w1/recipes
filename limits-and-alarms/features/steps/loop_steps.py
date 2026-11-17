"""Steps for the runaway-loop extension and the local run. Nothing here calls AWS."""

from __future__ import annotations

import io
import json
import shutil
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

from agent_loop import SYSTEM_PROMPT_TOKENS, TOOL_RESULT_TOKENS, Limits, deadline_for, run_agent
from alarm_eval import evaluate
from alarms import alarm_specs
from behave import given, then, when
from fake_gateway import FakeGateway, GatewayRefused, runaway
from support import run_main

from config import Settings


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


class Recording:
    """The stand-in gateway, with a note of when each call started and how large its context was."""

    def __init__(self, gw, clock):
        self.gw, self.clock = gw, clock
        self.model = gw.model
        self.starts, self.contexts = [], []

    def __call__(self, context_tokens, max_tokens):
        self.starts.append(self.clock())
        self.contexts.append(context_tokens)
        return self.gw(context_tokens, max_tokens)


def make(context, budget, call_s=1.0):
    context.clock = Clock()
    context.gw = Recording(FakeGateway(runaway, budget_cad=budget, clock=context.clock, call_s=call_s), context.clock)


@given('a model that always asks for another tool call, behind a stand-in gateway with a budget of {budget:g} CAD')
def runaway_model(context, budget):
    make(context, budget)


@given('a model that always asks for another tool call and takes {s:d} seconds a call, behind a stand-in gateway with a budget of {budget:g} CAD')
def slow_model(context, s, budget):
    make(context, budget, call_s=s)


@given('a model that always asks for another tool call, behind a stand-in gateway that refuses every call with "{code}"')
def refusing(context, code):
    context.clock = Clock()

    def refuse(context_tokens, max_tokens):
        context.gw.starts.append(context.clock())
        raise GatewayRefused(code, "refused by the scenario")
    refuse.model = Limits().model
    context.gw = SimpleNamespace(starts=[], contexts=[])
    context.gw_call = refuse


def run_task(context, limits):
    context.emf = io.StringIO()
    call = getattr(context, "gw_call", None) or getattr(context, "gw_direct", None) or context.gw
    context.error = None
    try:
        context.result = run_agent("a task", call_model=call, tools={"search": lambda q: f"no result for {q}"},
                                   limits=limits, clock=context.clock, out=context.emf)
    except ValueError as e:
        context.error = e


@when('the loop runs one task with the default limits')
def default_limits(context):
    run_task(context, Limits())


@when('the loop runs one task with a step cap of {n:d}')
def step_cap(context, n):
    run_task(context, Limits(max_steps=n))


@when('the loop runs one task with no step cap and no deadline')
def no_cap_no_deadline(context):
    run_task(context, Limits(max_steps=None, deadline_s=None))


@when('the loop runs one task with no step cap, no deadline and no cost cap')
def no_limits(context):
    run_task(context, Limits(max_steps=None, deadline_s=None, max_cost_cad=None))


@then('the next model call could have taken the task past {cad:g} CAD')
def next_call_past(context, cad):
    from config import cost_cad

    limits = Limits()
    context_tokens = SYSTEM_PROMPT_TOKENS + TOOL_RESULT_TOKENS * limits.keep_messages
    assert context.result.cost_cad + cost_cad(limits.model, context_tokens, limits.max_tokens) > cad, context.result


@then('the task stopped with "{reason}" after {n:d} model calls')
def stopped_after(context, reason, n):
    assert (context.result.stop_reason, context.result.steps) == (reason, n), context.result


@then('the task stopped with "{reason}"')
def stopped(context, reason):
    assert context.result.stop_reason == reason, context.result


@then('every model call started less than {s:d} seconds after the task started')
def started_before(context, s):
    assert context.gw.starts and max(context.gw.starts) < s, context.gw.starts


@then('the gateway was asked {n:d} time')
def asked(context, n):
    assert len(context.gw.starts) == n, context.gw.starts


@then('the context of model call {k:d} holds {n:d} tool results')
def context_holds(context, k, n):
    assert context.gw.contexts[k - 1] == SYSTEM_PROMPT_TOKENS + n * TOOL_RESULT_TOKENS, context.gw.contexts


@then('the task wrote {n:d} metric line with StopReason "{reason}", AgentSteps {steps:d} and Errors {e:d}')
def metric_line(context, n, reason, steps, e):
    lines = [json.loads(x) for x in context.emf.getvalue().splitlines()]
    assert len(lines) == n, lines
    context.line = lines[0]
    assert (context.line["StopReason"], context.line["AgentSteps"], context.line["Errors"]) == (reason, steps, e)


@then('the line has a second directive with the dimensions "{dims}" and the one metric "{metric}"')
def second_directive(context, dims, metric):
    d = context.line["_aws"]["CloudWatchMetrics"]
    assert len(d) == 2 and d[1]["Dimensions"] == [[x.strip() for x in dims.split(",")]], d
    assert [m["Name"] for m in d[1]["Metrics"]] == [metric], d


@given('Lambda has {ms:d} ms left for the function')
def lambda_left(context, ms):
    context.lambda_context = SimpleNamespace(get_remaining_time_in_millis=lambda: ms)


@then("the loop's deadline for a cap of {cap:d} seconds is {s:g} seconds")
def deadline(context, cap, s):
    assert abs(deadline_for(context.lambda_context, cap) - s) < 1e-9, deadline_for(context.lambda_context, cap)


@then('the task spent at most {cad:g} CAD')
def spent(context, cad):
    assert 0 < context.result.cost_cad <= cad, context.result


@when('ext-runaway-loop/loop_run.py runs with an EMF file in a temporary folder')
def loop_run(context):
    tmp = Path(tempfile.mkdtemp(prefix="limits-loop-"))
    context.undo.append(lambda: shutil.rmtree(tmp, ignore_errors=True))
    context.emf_file = tmp / "loop-emf.jsonl"
    run_main(context, "ext-runaway-loop/loop_run.py", ["--emf", str(context.emf_file)])


@then('the printed lines are')
@then('the printed lines are:')
def printed_lines(context):
    assert context.printed.rstrip("\n").splitlines() == context.text.splitlines(), context.printed


@then('the EMF file has {n:d} lines')
def emf_file(context, n):
    assert len(context.emf_file.read_text().splitlines()) == n


@then('the daily-cost alarm, checked against that task\'s metric line, is "{state}"')
def daily_cost(context, state):
    s = Settings()
    doc = json.loads(context.emf.getvalue().splitlines()[0])
    now = int(time.time() * 1000)
    doc["Service"], doc["_aws"]["Timestamp"] = s.service, now - 1000
    for d in doc["_aws"]["CloudWatchMetrics"]:
        d["Namespace"] = s.namespace
    spec = [x for x in alarm_specs(s, "topic") if x["MetricName"] == "CostCAD"][0]
    assert evaluate(spec, [doc], now)[0] == state, (context.result, evaluate(spec, [doc], now))


@then('the printed lines from "{start}" on are:')
def lines_from(context, start):
    lines = context.printed.rstrip("\n").splitlines()
    i = next(k for k, ln in enumerate(lines) if ln.startswith(start))
    assert lines[i:] == context.text.splitlines(), "\n".join(lines)


@given('ten minutes of traffic, 10 requests a minute, with 2 slow requests in each of the last {k:d} minutes')
def slow_minutes(context, k):
    from emf import request_line

    s = Settings()
    context.now = int(time.time() * 1000)
    context.docs = []
    for minute in range(10, 0, -1):
        for i in range(10):
            slow = minute <= k and i < 2
            context.docs.append(request_line(s.namespace, s.service, latency_ms=6500 if slow else 400, error=False,
                                             cost_cad=0.0, ts_ms=context.now - minute * 60_000 + i * 5000 + 1))


@when('main/alarm_eval.py checks the p95 latency alarm against it')
def check_p95(context):
    spec = [x for x in alarm_specs(Settings(), "topic") if x["MetricName"] == "LatencyMs"][0]
    context.state = evaluate(spec, context.docs, context.now)[0]


@then('the state is "{state}"')
def state_is(context, state):
    assert context.state == state, context.state


@given('a model that always asks for another tool call, behind a stand-in gateway for "{model}" with a budget of {budget:g} CAD')
def other_model(context, model, budget):
    context.clock = Clock()
    context.gw_direct = FakeGateway(runaway, budget_cad=budget, clock=context.clock, model=model)
    context.gw = SimpleNamespace(starts=[], contexts=[])


@then('the loop stopped before any call with a message that names "{a}" and "{b}"')
def refused_models(context, a, b):
    assert context.error is not None and a in str(context.error) and b in str(context.error), context.error
    assert context.gw_direct.calls == 0


@given('a gateway client for the runaway model that has no model attribute')
def no_model(context):
    context.clock = Clock()
    gw = FakeGateway(runaway, budget_cad=1.0, clock=context.clock)
    context.gw_direct = SimpleNamespace(calls=0)

    def call(context_tokens, max_tokens):
        context.gw_direct.calls += 1
        return gw(context_tokens, max_tokens)
    context.gw_call = call
    context.gw = SimpleNamespace(starts=[], contexts=[])

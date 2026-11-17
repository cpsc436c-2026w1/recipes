"""ext-runaway-loop: the loop's own limits stop a runaway; when they fail, the gateway budget stops it."""

from __future__ import annotations

import io
import json
import time
from types import SimpleNamespace

import pytest

from agent_loop import Limits, deadline_for, run_agent
from alarm_eval import evaluate
from alarms import alarm_specs
from fake_gateway import FakeGateway, GatewayRefused, finishes_after, runaway


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


TOOLS = {"search": lambda q: f"nothing for {q}"}


def go(behaviour, limits, budget=1.0, call_s=1.0, rate=60):
    clock, out = Clock(), io.StringIO()
    gw = FakeGateway(behaviour, budget_cad=budget, clock=clock, call_s=call_s, rate_per_min=rate)
    r = run_agent("t", call_model=gw, tools=TOOLS, limits=limits, clock=clock, out=out)
    return r, gw, [json.loads(x) for x in out.getvalue().splitlines()]


def test_normal_task_finishes():
    r, _, docs = go(finishes_after(2), Limits())
    assert (r.stop_reason, r.steps) == ("done", 3)
    assert docs[0]["Errors"] == 0 and docs[0]["StopReason"] == "done"
    for d in docs[0]["_aws"]["CloudWatchMetrics"]:
        assert all(isinstance(docs[0][m["Name"]], (int, float)) for m in d["Metrics"])
        assert all(isinstance(docs[0][k], str) for ds in d["Dimensions"] for k in ds)
    assert len(docs[0]["_aws"]["CloudWatchMetrics"][1]["Metrics"]) == 1  # one metric per stop reason, not six


def test_step_cap_stops_runaway():
    r, gw, docs = go(runaway, Limits(max_steps=8))
    assert (r.stop_reason, r.steps, gw.calls) == ("step_cap", 8, 8)
    assert docs[0]["Errors"] == 1 and docs[0]["AgentSteps"] == 8


def test_deadline_stops_slow_runaway():
    r, _, _ = go(runaway, Limits(max_steps=1000, deadline_s=20), call_s=4.0)
    assert r.stop_reason == "deadline" and r.seconds <= 20 + 4  # no call starts after the deadline


def test_gateway_budget_stops_runaway_when_cap_fails():
    r, gw, _ = go(runaway, Limits(max_steps=None, deadline_s=None, max_cost_cad=None), budget=0.05)
    assert r.stop_reason == "budget_exhausted"
    assert gw.spent <= 0.05  # the reserve check refuses before the budget is passed, never after


def test_gateway_rate_limit_stops_a_fast_runaway():
    r, _, _ = go(runaway, Limits(max_steps=None, deadline_s=None, max_cost_cad=None), call_s=0.1, rate=60)
    assert r.stop_reason == "rate_limited" and r.steps == 60


def test_no_retry_after_refusal():
    calls = []

    def refusing(context, max_tokens):
        calls.append(1)
        raise GatewayRefused("budget_exhausted", "spent")

    refusing.model = Limits().model  # the model the cost cap prices (R70)
    r = run_agent("t", call_model=refusing, tools=TOOLS, limits=Limits(), clock=Clock(), out=io.StringIO())
    assert r.stop_reason == "budget_exhausted" and len(calls) == 1


def test_context_is_capped():
    seen = []
    gw = FakeGateway(runaway, clock=Clock())

    def spy(context, max_tokens):
        seen.append(context)
        return gw(context, max_tokens)

    spy.model = Limits().model  # the model the cost cap prices (R70)
    run_agent("t", call_model=spy, tools=TOOLS, limits=Limits(max_steps=30, keep_messages=10), clock=gw.clock,
              out=io.StringIO())
    assert max(seen) == seen[10] == seen[-1]  # context stops growing after 10 tool results


def test_deadline_never_later_than_lambda_allows():
    ctx = SimpleNamespace(get_remaining_time_in_millis=lambda: 9_000)
    assert deadline_for(ctx, cap_s=20) == pytest.approx(7.0)
    assert deadline_for(None, cap_s=20) == 20


def test_runaway_at_full_key_budget_fires_daily_cost_alarm(s):
    """With every in-code limit broken, a runaway spends the stand-in's whole budget of 1 CAD; the daily-cost alarm sees it."""
    r, _, docs = go(runaway, Limits(max_steps=None, deadline_s=None, max_cost_cad=None, keep_messages=10), budget=1.0, call_s=1.0)
    assert r.stop_reason == "budget_exhausted" and 0.95 < r.cost_cad <= 1.0
    now = int(time.time() * 1000)
    for d in docs:
        d["Service"], d["_aws"]["Timestamp"] = s.service, now - 1000
        d["_aws"]["CloudWatchMetrics"][0]["Namespace"] = s.namespace
    daily = [x for x in alarm_specs(s, "t") if x["MetricName"] == "CostCAD"][0]
    assert evaluate(daily, docs, now)[0] == "ALARM"

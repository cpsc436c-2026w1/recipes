"""Main scenario: EMF lines are valid, each alarm's condition fires on its traffic and only there, limits are set."""

from __future__ import annotations

import io
import json
import time

import pytest

import run
from alarm_eval import evaluate, statistic
from alarms import alarm_specs
from app import handler
from config import ALARMS, LAMBDA_TIMEOUT_S, MONTHLY_BUDGET_USD, RESERVED_CONCURRENCY, cost_cad
from emf import request_line
from limits import concurrency_fits


def lines_of(buf: io.StringIO) -> list[dict]:
    return [json.loads(x) for x in buf.getvalue().splitlines()]


# --- EMF -----------------------------------------------------------------------------------------------------

def check_emf(doc: dict) -> None:
    """The rules of the EMF specification that a mistake in app code breaks most often."""
    meta = doc["_aws"]
    assert isinstance(meta["Timestamp"], int)
    for d in meta["CloudWatchMetrics"]:
        assert 1 <= len(d["Namespace"]) <= 255
        for dimset in d["Dimensions"]:
            assert len(dimset) <= 30
            for key in dimset:
                assert isinstance(doc[key], str) and doc[key]  # every dimension is a string on the root
        assert len(d["Metrics"]) <= 100
        for m in d["Metrics"]:
            assert isinstance(doc[m["Name"]], (int, float))  # every metric is a number on the root
    dims = {k for d in meta["CloudWatchMetrics"] for ds in d["Dimensions"] for k in ds}
    assert "requestId" not in dims  # a per-request dimension would create one billed metric per request


def test_handler_writes_one_valid_line_per_request_also_on_failure():
    out = io.StringIO()
    ok = handler({"delay_ms": 1}, None, out=out)
    bad = handler({"delay_ms": 1, "fail": True}, None, out=out)
    assert (ok["statusCode"], bad["statusCode"]) == (200, 500)
    docs = lines_of(out)
    assert len(docs) == 2
    for d in docs:
        check_emf(d)
    assert [d["Errors"] for d in docs] == [0, 1]
    assert docs[0]["CostCAD"] == pytest.approx(cost_cad("qwen3.6-35b-a3b", 800, 120))
    assert docs[1]["CostCAD"] == 0  # the failed call returned no tokens


def test_handler_line_is_one_json_line():
    out = io.StringIO()
    handler({"delay_ms": 1}, None, out=out)
    assert out.getvalue().count("\n") == 1 and out.getvalue().startswith('{"_aws"')


# --- alarms: definitions -------------------------------------------------------------------------------------

def test_alarm_definitions_in_cloudwatch(s, deployed):
    cw = deployed["c"]["cw"]
    got = {a["AlarmName"]: a for a in cw.describe_alarms(AlarmNames=deployed["names"])["MetricAlarms"]}
    assert set(got) == {s.alarm_name(x) for x in ALARMS}
    e, p, d = got[s.alarm_name("errors")], got[s.alarm_name("p95-latency")], got[s.alarm_name("daily-cost")]
    assert (e["MetricName"], e["Statistic"], e["Period"], e["Threshold"]) == ("Errors", "Sum", 300, 5)
    assert (p["ExtendedStatistic"], p["Period"], p["EvaluationPeriods"], p["DatapointsToAlarm"]) == ("p95", 60, 3, 2)
    assert (d["MetricName"], d["Period"], d["Threshold"]) == ("CostCAD", 86400, 0.5)
    for a in got.values():
        if a["Namespace"] == "AWS/Lambda":  # Lambda's own metrics of the function
            assert a["Dimensions"] == [{"Name": "FunctionName", "Value": s.function_name}], a
        else:
            assert a["Namespace"] == s.namespace and a["Dimensions"] == [{"Name": "Service", "Value": s.service}]
        assert a["AlarmActions"] == [deployed["topic"]] and a["TreatMissingData"] == "notBreaching"


def test_alarm_metrics_match_what_the_app_emits(s):
    doc = request_line(s.namespace, s.service, latency_ms=1, error=False, cost_cad=0)
    emitted = {m["Name"] for m in doc["_aws"]["CloudWatchMetrics"][0]["Metrics"]}
    for spec in alarm_specs(s, "arn:topic"):
        if spec["Namespace"] == "AWS/Lambda":
            continue  # Lambda writes these itself
        assert spec["MetricName"] in emitted and spec["Namespace"] == doc["_aws"]["CloudWatchMetrics"][0]["Namespace"]


# --- alarms: each condition fires on its traffic, and only there ----------------------------------------------

@pytest.mark.parametrize("kind,fires", [("normal", set()), ("errors", {"errors"}), ("slow", {"p95-latency"}),
                                        ("expensive", {"daily-cost"})])
def test_each_alarm_fires_on_its_traffic(s, kind, fires):
    now = int(time.time() * 1000)
    docs = run.synthetic(s, now, kind)
    for d in docs[:3]:
        check_emf(d)
    for slug, spec in zip(ALARMS, alarm_specs(s, "arn:topic")):
        state, _ = evaluate(spec, docs, now)
        assert state == ("ALARM" if slug in fires else "OK"), (kind, slug)


def test_latency_needs_two_of_three_minutes(s):
    now = 10 * 60_000 * 1000
    spec = alarm_specs(s, "arn:t")[1]

    def minute(m, latency):
        return [request_line(s.namespace, s.service, latency_ms=latency, error=False, cost_cad=0,
                             ts_ms=now - m * 60_000 + i * 1000) for i in range(20)]

    one_slow = minute(3, 400) + minute(2, 400) + minute(1, 9000)
    two_slow = minute(3, 9000) + minute(2, 400) + minute(1, 9000)
    assert evaluate(spec, one_slow, now)[0] == "OK"      # one slow minute is not enough
    assert evaluate(spec, two_slow, now)[0] == "ALARM"   # 2 of the last 3


def test_other_service_does_not_count(s):
    now = int(time.time() * 1000)
    docs = [request_line(s.namespace, "someone-else", latency_ms=1, error=True, cost_cad=0, ts_ms=now - 1000)
            for _ in range(10)]
    assert evaluate(alarm_specs(s, "t")[0], docs, now)[0] == "OK"


def test_nearest_rank_percentile():
    assert statistic(list(range(1, 101)), "p95") == 95
    assert statistic([1, 2, 3], "Sum") == 6


def test_set_alarm_state_path(s, deployed):
    import fire

    lines = fire.notification_path(s, deployed["c"]["cw"], read_history=False)
    assert all("state ALARM" in x for x in lines) and len(lines) == len(ALARMS)
    states = {a["StateValue"] for a in deployed["c"]["cw"].describe_alarms(AlarmNames=deployed["names"])["MetricAlarms"]}
    assert states == {"OK"}  # set back after the test


# --- limits outside the code path ------------------------------------------------------------------------------

def test_budget_with_alerts_to_topic(s, deployed):
    b = deployed["c"]["budgets"]
    acct = deployed["c"]["sts"].get_caller_identity()["Account"]
    budget = b.describe_budget(AccountId=acct, BudgetName=s.budget_name)["Budget"]
    assert float(budget["BudgetLimit"]["Amount"]) == MONTHLY_BUDGET_USD and budget["TimeUnit"] == "MONTHLY"
    notes = b.describe_notifications_for_budget(AccountId=acct, BudgetName=s.budget_name)["Notifications"]
    assert {(n["NotificationType"], n["Threshold"]) for n in notes} == {
        ("ACTUAL", 50.0), ("ACTUAL", 80.0), ("ACTUAL", 100.0), ("FORECASTED", 100.0)}


def test_topic_lets_budgets_publish(s, deployed):
    attrs = deployed["c"]["sns"].get_topic_attributes(TopicArn=deployed["topic"])["Attributes"]
    principals = {st["Principal"]["Service"] for st in json.loads(attrs["Policy"])["Statement"]}
    assert "budgets.amazonaws.com" in principals


def test_function_concurrency_and_timeout(s, deployed):
    lam = deployed["c"]["lam"]
    assert lam.get_function_concurrency(FunctionName=s.function_name)["ReservedConcurrentExecutions"] == \
        RESERVED_CONCURRENCY
    conf = lam.get_function_configuration(FunctionName=s.function_name)
    assert conf["Timeout"] == LAMBDA_TIMEOUT_S and conf["Handler"] == "app.handler"


def test_concurrency_refused_in_small_account():
    small = {"AccountLimit": {"ConcurrentExecutions": 10, "UnreservedConcurrentExecutions": 10}}
    default = {"AccountLimit": {"ConcurrentExecutions": 1000, "UnreservedConcurrentExecutions": 1000}}
    assert not concurrency_fits(small, 5)
    assert concurrency_fits(default, 5)
    assert concurrency_fits({"AccountLimit": {"ConcurrentExecutions": 1000, "UnreservedConcurrentExecutions": 100}},
                            5, current=5)  # re-running counts the function's own reservation back in


def test_dashboard_shows_every_alarm_and_metric(s, deployed):
    body = json.loads(deployed["c"]["cw"].get_dashboard(DashboardName=s.dashboard_name)["DashboardBody"])
    text = json.dumps(body)
    for m in ("Requests", "Errors", "LatencyMs", "CostCAD", "Throttles"):
        assert f'"{m}"' in text
    alarm_widget = [w for w in body["widgets"] if w["type"] == "alarm"][0]
    assert len(alarm_widget["properties"]["alarms"]) == len(ALARMS)


def test_set_up_twice_changes_nothing(s, deployed):
    role = deployed["c"]["lam"].get_function_configuration(FunctionName=s.function_name)["Role"]
    lines, _, _ = run.set_up(s, deployed["c"], role, run.DEFAULT_ACCOUNT)
    assert any("already there" in x for x in lines) and any("updated" in x for x in lines)


# --- failures that must stay visible ---------------------------------------------------------------------------

def test_request_for_a_model_without_a_price_is_counted_as_an_error_before_any_work(monkeypatch):
    import app

    worked = []
    monkeypatch.setattr(app, "do_work", lambda event: worked.append(event) or {"reply": "ok"})
    out = io.StringIO()
    reply = handler({"model": "some-new-model", "delay_ms": 1}, None, out=out)
    docs = lines_of(out)
    assert reply["statusCode"] == 500 and worked == []  # refused before the model would be called
    assert len(docs) == 1  # the line is still written: the errors alarm sees it
    check_emf(docs[0])
    assert (docs[0]["Errors"], docs[0]["CostCAD"], docs[0]["errorType"]) == (1, 0, "KeyError")
    assert "errorType" not in {m["Name"] for m in docs[0]["_aws"]["CloudWatchMetrics"][0]["Metrics"]}


class FakeLambda:
    def __init__(self, broken: bool):
        self.broken, self.payloads = broken, []

    def invoke(self, FunctionName, Payload):
        self.payloads.append(json.loads(Payload))
        if self.broken:
            return {"StatusCode": 200, "FunctionError": "Unhandled",
                    "Payload": io.BytesIO(b'{"errorMessage": "Unable to import module app"}')}
        code = 500 if self.payloads[-1]["fail"] else 200   # the handler's answer to a failed request is a 500
        return {"StatusCode": 200, "Payload": io.BytesIO(json.dumps({"statusCode": code}).encode())}


class HandlerLambda:
    """Invoke runs main/app.py's handler, as Lambda would; `model` is put into every event."""

    def __init__(self, model=None):
        self.model = model

    def invoke(self, FunctionName, Payload):
        from app import handler

        event = json.loads(Payload)
        if self.model:
            event["model"] = self.model
        out = handler(event, None, out=io.StringIO())
        return {"StatusCode": 200, "Payload": io.BytesIO(json.dumps(out).encode())}


def test_send_requests_counts_the_status_of_each_reply(s):
    # The handler as deployed: 10 answer 200 and the 2 sent to fail answer 500; the line says so.
    assert run.send_requests(s, HandlerLambda()) == (
        "sent 12 requests (2 failing); metrics reach the dashboard in a few minutes")
    # A model missing from the price table: every request answers 500, also the ones that should succeed.
    with pytest.raises(SystemExit) as e:
        run.send_requests(s, HandlerLambda(model="some-unpriced-model"))
    msg = str(e.value)
    assert "12 answered 500" in msg and "10 did not answer as planned" in msg
    assert "request 1 (sent to succeed) answered 500" in msg


def test_real_requests_that_break_the_function_stop_the_run(s):
    ok = FakeLambda(broken=False)
    assert run.send_requests(s, ok) == "sent 12 requests (2 failing); metrics reach the dashboard in a few minutes"
    assert sum(p["fail"] for p in ok.payloads) == 2
    with pytest.raises(SystemExit) as e:
        run.send_requests(s, FakeLambda(broken=True))
    assert "12 of 12 requests broke the function" in str(e.value) and "Unable to import module" in str(e.value)


def test_second_run_repairs_a_role_left_without_its_policy(s):
    """A first run that stopped between CreateRole and PutRolePolicy."""
    from limits import LOGS_POLICY_NAME, ensure_role

    c = run.clients(s)
    arn, _ = ensure_role(s, c["iam"], "123456789012")
    c["iam"].delete_role_policy(RoleName=s.role_name, PolicyName=LOGS_POLICY_NAME)
    again, line = ensure_role(s, c["iam"], "123456789012")
    assert again == arn and "already there" in line
    assert c["iam"].list_role_policies(RoleName=s.role_name)["PolicyNames"] == [LOGS_POLICY_NAME]

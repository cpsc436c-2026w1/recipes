"""ext-sdk-lambda offline: the handler with scripted models for both SDKs, the checks on the value and the reply,
the read-only tool, the caps and the deadline, the gateway's refusals against a stand-in server."""
from __future__ import annotations

import asyncio
import json
import os
import time

import pytest

from tests.sdk_lambda_fakes import RefusingGateway, calls_of, cancelled_of, ext_env, load, scripted, trips_file

SDKS = ["strands", "pydantic-ai"]
WHOLE_DAY = [("sql", "SELECT COUNT(*) AS trips FROM trips"),
             ("output", {"reply": "There were 19074 trips.", "value": 19074})]


@pytest.fixture(scope="session")
def trips(tmp_path_factory):
    return trips_file(tmp_path_factory.mktemp("sim01"))


@pytest.fixture
def es(monkeypatch):
    ext_env()
    return load("settings").load_settings()


@pytest.fixture
def eng(trips):
    return load("engine").Engine(trips)


def ask(es, eng, question, script, sdk="strands", **kw):
    handler = load("handler", "sdk_handler")
    model = scripted(sdk, script, kw.pop("scale", 1.0))
    result = asyncio.run(handler.answer_async(question, es, sdk=sdk, model=model, eng=eng, **kw))
    return result, model


@pytest.mark.parametrize("sdk", SDKS)
def test_whole_day_is_answered_with_sql_and_rows_from_the_tool_log(es, eng, sdk):
    r, _ = ask(es, eng, "How many trips were requested on 2024-03-14?", WHOLE_DAY, sdk)
    assert (r["status"], r["value"], r["sql"], r["rows"]) == \
        ("answered", 19074, "SELECT COUNT(*) AS trips FROM trips", [{"trips": 19074}])
    assert (r["model_calls"], r["tool_calls"]) == (2, 1)


@pytest.mark.parametrize("sdk", SDKS)
def test_a_rounded_value_is_not_shown(es, eng, sdk):
    r, _ = ask(es, eng, "How many trips were requested on 2024-03-14?",
               [("sql", "SELECT COUNT(*) FROM trips"), ("output", {"reply": "About 19000 trips.", "value": 19000})], sdk)
    assert (r["status"], r["reason"], r["value"]) == ("did_not_answer", "value_not_in_result", None)


@pytest.mark.parametrize("sdk", SDKS)
def test_no_value_keeps_the_reply(es, eng, sdk):
    r, _ = ask(es, eng, "What did riders tip in zone 161?",
               [("output", {"reply": "The trips table has no tips.", "value": None})], sdk)
    assert (r["status"], r["reason"], r["reply"], r["value"]) == \
        ("did_not_answer", "no_value", "The trips table has no tips.", None)


@pytest.mark.parametrize("sdk", SDKS)
def test_a_reply_that_rounds_the_value_disagrees(es, eng, sdk):
    r, _ = ask(es, eng, "How many trips were requested on 2024-03-14?",
               [("sql", "SELECT COUNT(*) AS trips FROM trips"),
                ("output", {"reply": "There were about 19000 trips.", "value": 19074})], sdk)
    assert (r["status"], r["reason"], r["value"]) == ("did_not_answer", "reply_disagrees", None)


def test_numbers_of_the_question_and_the_sql_are_allowed(es, eng):
    sql = "SELECT COUNT(*) FROM trips WHERE pickup_zone_id = 161 AND hour(requested_time) = 8"
    r, _ = ask(es, eng, "How many trips were requested in zone 161 between 08:00 and 09:00?",
               [("sql", sql), ("output", {"reply": "191 trips were requested in zone 161 between 08:00 and 09:00.",
                                          "value": 191})])
    assert (r["status"], r["value"]) == ("answered", 191)


def test_the_reply_check_reads_thousands_separators_and_decimals():
    v = load("verdict")
    assert v.numbers_in("19,074 trips, 12.50 dollars, 3 zones") == [19074, 12.5, 3]
    assert v.states("There were 19,074 trips.", 19074) and not v.states("About 19 thousand", 19074)
    assert v.states("The mean fare was 21.37 dollars.", 21.3712) and not v.states("about 21 dollars", 21.3712)


@pytest.mark.parametrize("sql", ["DELETE FROM trips", "SELECT COUNT(*) FROM trips; DROP TABLE trips",
                                 "SELECT * FROM read_csv('/etc/passwd')", "COPY trips TO '/tmp/trips.csv'"])
def test_the_tool_rejects_what_is_not_one_select(eng, sql):
    toollog = load("toollog")
    tool = toollog.SqlTool(eng, toollog.Deadline(25))
    assert "rejected" in tool(sql)["error"]
    assert eng.statements_run == 0 and eng.count() == 19074


@pytest.mark.parametrize("sql", ["SELECT 7661 - 7068 AS difference", "SELECT 19074",
                                 "WITH t AS (SELECT 593 AS n) SELECT n FROM t",
                                 "WITH trips AS (SELECT 19074 AS n) SELECT n FROM trips"])
def test_the_tool_rejects_a_select_that_does_not_read_trips(eng, sql):
    # live run 2026-11-22: Pydantic AI sent SELECT 7661 - 7068 and its 593 passed the value check as a query result
    toollog = load("toollog")
    tool = toollog.SqlTool(eng, toollog.Deadline(25))
    assert tool(sql)["error"].startswith("rejected") and eng.statements_run == 0
    assert tool.succeeded == []


@pytest.mark.parametrize("sql", ["SELECT 7661 - 7068 AS difference FROM trips LIMIT 1",
                                 "SELECT 161 AS zone, COUNT(*) AS n FROM trips WHERE pickup_zone_id = 161",
                                 "SELECT n FROM (SELECT 593 AS n FROM trips LIMIT 1)",
                                 "WITH t AS (SELECT 19074 AS n FROM trips LIMIT 1) SELECT n FROM t",
                                 "SELECT n FROM (VALUES (593)) v(n), trips LIMIT 1"])
def test_the_tool_rejects_a_constant_output_column_beside_a_read_of_trips(eng, sql):
    # the read of trips alone is not enough: the constant's number would sit in the rows and pass the value check
    toollog = load("toollog")
    tool = toollog.SqlTool(eng, toollog.Deadline(25))
    assert tool(sql)["error"].startswith("rejected") and eng.statements_run == 0
    assert tool.succeeded == []


@pytest.mark.parametrize("sql", ["SELECT COUNT(1) AS n FROM trips", "SELECT 'zone 161' AS label, COUNT(*) FROM trips",
                                 "SELECT ROUND(AVG(fare), 2) FROM trips", "SELECT pickup_zone_id, COUNT(*) FROM trips "
                                 "GROUP BY 1 ORDER BY 2 DESC LIMIT 1",
                                 "SELECT COUNT(*) FROM trips t WHERE EXISTS (SELECT 1 FROM trips u "
                                 "WHERE u.pickup_zone_id = t.pickup_zone_id AND u.fare > 50)"])
def test_the_tool_runs_constants_that_sit_inside_a_read_of_the_data(eng, sql):
    toollog = load("toollog")
    tool = toollog.SqlTool(eng, toollog.Deadline(25))
    assert "error" not in tool(sql) and len(tool.succeeded) == 1


def test_the_tool_runs_a_computed_number_read_from_trips_in_subqueries(eng):
    toollog = load("toollog")
    tool = toollog.SqlTool(eng, toollog.Deadline(25))
    result = tool("SELECT (SELECT COUNT(*) FROM trips WHERE pickup_zone_id = 161) - "
                  "(SELECT COUNT(*) FROM trips WHERE pickup_zone_id = 230) AS more")
    assert result["rows"] == [[593]]


@pytest.mark.parametrize("sdk", SDKS)
def test_a_number_the_model_put_in_a_query_of_constants_is_not_shown(es, eng, sdk):
    r, _ = ask(es, eng, "How many more trips were requested in zone 161 than in zone 230 on the whole day?",
               [("sql", "SELECT 7661 - 7068 AS difference"),
                ("output", {"reply": "There were 593 more trips.", "value": 593})], sdk)
    assert (r["status"], r["reason"], r["value"]) == ("did_not_answer", "value_not_in_result", None)
    assert r["log"][0]["status"] == "rejected"


TWO_ZONES = [("sql", "SELECT COUNT(*) AS n FROM trips WHERE pickup_zone_id = 161"),
             ("sql", "SELECT COUNT(*) AS n FROM trips WHERE pickup_zone_id = 230")]
DIFFERENCE = "How many more trips were requested in zone 161 than in zone 230 on the whole day?"


@pytest.mark.parametrize("sdk", SDKS)
def test_a_difference_of_two_counts_the_tool_returned_is_answered(es, eng, sdk):
    # live run 2026-11-22: the model queried 7661 and 7068 and stated 593; the value check withheld it
    r, _ = ask(es, eng, DIFFERENCE, TWO_ZONES + [
        ("output", {"reply": "There were 593 more trips in zone 161 than in zone 230.", "value": 593})], sdk)
    assert (r["status"], r["value"], r["derived"]) == ("answered", 593, "difference of 7661 and 7068")
    assert r["rows"] == [{"n": 7661}, {"n": 7068}] and r["sql"].count("SELECT") == 2


@pytest.mark.parametrize("sdk", SDKS)
def test_a_number_no_result_holds_or_gives_is_still_withheld(es, eng, sdk):
    r, _ = ask(es, eng, DIFFERENCE, TWO_ZONES + [
        ("output", {"reply": "There were 600 more trips in zone 161 than in zone 230.", "value": 600})], sdk)
    assert (r["status"], r["reason"], r["value"]) == ("did_not_answer", "value_not_in_result", None)


def test_a_row_count_and_a_column_total_of_a_complete_result_count_as_derived():
    v = load("verdict")
    log = [{"sql": "SELECT pickup_zone_id, COUNT(*) AS n FROM trips GROUP BY 1", "status": "ok",
            "columns": ["pickup_zone_id", "n"], "rows": [[48, 4345], [161, 7661], [230, 7068]], "truncated": False}]
    assert v.derivation(3, log)[0].startswith("row count")
    assert v.derivation(19074, log)[0] == "total of column n"
    assert v.derivation(19075, log) is None
    assert v.derivation(19074, [{**log[0], "truncated": True}]) is None


def test_the_engine_refuses_files_even_past_the_check(eng):
    import duckdb

    with pytest.raises(duckdb.Error):
        eng.run("SELECT * FROM read_csv('/etc/passwd')")
    with pytest.raises(duckdb.Error):
        eng.run("SET enable_external_access = true")


@pytest.mark.parametrize("sdk", SDKS)
def test_the_same_question_with_each_sdk_validates_as_trip_answer(es, eng, sdk):
    r, _ = ask(es, eng, "How many trips were requested on 2024-03-14?", WHOLE_DAY, sdk)
    load("trip_contracts").TripAnswer.model_validate({"reply": r["reply"], "value": r["value"]})
    assert r["sdk"] == sdk and r["status"] == "answered"


@pytest.mark.parametrize("sdk", SDKS)
def test_a_model_that_never_stops_is_stopped_at_4_model_calls_and_3_queries(es, eng, sdk):
    r, model = ask(es, eng, "How many trips?", [("sql", "SELECT COUNT(*) FROM trips")], sdk)
    assert (r["status"], r["model_calls"], r["tool_calls"], r["value"]) == ("stopped", 4, 3, None)
    assert calls_of(model) == 4


@pytest.mark.parametrize("sdk", SDKS)
def test_a_question_of_three_queries_is_answered(es, eng, sdk):
    script = [("sql", f"SELECT COUNT(*) FROM trips WHERE pickup_zone_id = {z}") for z in (48, 161, 230)]
    script.append(("output", {"reply": "Zone 161 had the most trips: 7661.", "value": 7661}))
    r, _ = ask(es, eng, "Which of the three zones had the most trips?", script, sdk)
    assert (r["status"], r["value"], r["model_calls"], r["tool_calls"]) == ("answered", 7661, 4, 3)
    assert r["sql"] == "SELECT COUNT(*) FROM trips WHERE pickup_zone_id = 161"


SCALE = 0.05     # the slow-model test runs 20 times faster: 12 s calls take 0.6 s, the deadline is 1.25 s


@pytest.mark.parametrize("sdk", SDKS)
def test_a_slow_model_is_cancelled_at_the_deadline(es, eng, sdk):
    s = es.model_copy(update={"deadline_s": 25 * SCALE})
    # a first question warms the SDK (imports, agent set-up), so the timed one measures the deadline, not the start
    ask(es, eng, "How many trips?", [("output", {"reply": "warm-up", "value": None})], sdk)
    start = time.monotonic()
    r, model = ask(s, eng, "How many trips?", [("sql", "SELECT COUNT(*) FROM trips", 12)] * 3, sdk, scale=SCALE)
    took = (time.monotonic() - start) / SCALE
    assert (r["status"], r["model_calls"]) == ("stopped", 3)
    assert cancelled_of(model) == 1 and took < 26


REFUSALS = [("pydantic-ai", 429, "team_budget_exhausted", None), ("pydantic-ai", 429, "rate_limit_exceeded", 60),
            ("pydantic-ai", 401, "invalid_api_key", None), ("pydantic-ai", 403, "model_not_allowed", None),
            ("pydantic-ai", 502, "upstream_error", None), ("strands", 429, "team_budget_exhausted", None),
            ("strands", 429, "rate_limit_exceeded", 60), ("strands", 403, "key_expired", None),
            ("strands", 502, "upstream_error", None)]


@pytest.mark.parametrize("sdk,status,code,retry_after", REFUSALS)
def test_a_refusal_of_the_gateway_is_did_not_answer_after_one_request(es, eng, sdk, status, code, retry_after):
    gw = RefusingGateway()
    gw.answer = (status, code, retry_after)
    try:
        s = es.model_copy(update={"course_gateway_url": gw.url})
        handler = load("handler", "sdk_handler")
        r = asyncio.run(handler.answer_async("How many trips?", s, sdk=sdk, eng=eng))
    finally:
        gw.close()
    assert (r["status"], r["value"], r["http_status"], r["code"], r["retry_after"]) == \
        ("did_not_answer", None, status, code, retry_after)
    assert len(gw.requests) == 1


@pytest.mark.parametrize("sdk", SDKS)
def test_prose_twice_is_output_format(es, eng, sdk):
    r, model = ask(es, eng, "How many trips?", [("prose", "about nineteen thousand"),
                                                ("prose", "roughly 19 thousand trips")], sdk)
    assert (r["status"], r["reason"], r["value"]) == ("did_not_answer", "output_format", None)
    assert calls_of(model) == 2


@pytest.mark.parametrize("sdk", SDKS)
def test_an_output_that_does_not_validate_twice_is_output_format(es, eng, sdk):
    r, model = ask(es, eng, "How many trips?", [("output", {"reply": "x"}), ("output", {"reply": "y", "value": "z"}),
                                                ("output", {"reply": "x"})], sdk)
    assert (r["status"], r["reason"], r["value"]) == ("did_not_answer", "output_format", None)
    assert calls_of(model) == 2


def test_the_handler_reads_a_function_url_event(es, eng, monkeypatch):
    handler = load("handler", "sdk_handler")
    monkeypatch.setitem(handler._CACHE, "settings", es)
    monkeypatch.setitem(handler._CACHE, "engine", eng)
    monkeypatch.setattr(handler, "model_for", lambda s, sdk: scripted(sdk, WHOLE_DAY))
    out = handler.handler({"body": json.dumps({"question": "How many trips were requested on 2024-03-14?"})}, None)
    body = json.loads(out["body"])
    assert out["statusCode"] == 200 and body["status"] == "answered" and body["value"] == 19074
    assert "log" not in body
    assert handler.handler({"body": "not json"}, None)["statusCode"] == 400


@pytest.mark.parametrize("sdk", SDKS)
def test_the_tokens_of_a_question_go_to_the_function_log_not_to_the_analyst(es, eng, monkeypatch, capsys, sdk):
    handler = load("handler", "sdk_handler")
    monkeypatch.setitem(handler._CACHE, "settings", es)
    monkeypatch.setitem(handler._CACHE, "engine", eng)
    monkeypatch.setattr(handler, "model_for", lambda s, chosen: scripted(chosen, WHOLE_DAY))
    r = asyncio.run(handler.answer_async("How many trips were requested on 2024-03-14?", es, sdk=sdk,
                                         model=scripted(sdk, WHOLE_DAY), eng=eng))
    assert isinstance(r["tokens_in"], int) and isinstance(r["tokens_out"], int)
    assert "tokens_in" not in handler.public(r) and "tokens_out" not in handler.public(r)
    out = handler.handler({"body": json.dumps({"question": "How many trips were requested on 2024-03-14?"})}, None)
    logged = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert {"tokens_in", "tokens_out"} <= logged.keys() and "tokens_in" not in json.loads(out["body"])


class Clock:
    """A monotonic clock a test moves: the deadline reads it, asyncio does not."""

    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.mark.parametrize("sdk", SDKS)
def test_past_the_deadline_the_next_tool_call_does_not_start(es, eng, sdk):
    clock = Clock()

    def late(n):
        if n == 1:
            clock.t += 30          # the first model call took 30 s; its tool call is about to start

    handler = load("handler", "sdk_handler")
    model = scripted(sdk, [("sql", "SELECT COUNT(*) FROM trips")], on_call=late)
    r = asyncio.run(handler.answer_async("How many trips?", es, sdk=sdk, model=model, eng=eng, clock=clock))
    assert (r["status"], r["model_calls"], r["tool_calls"], r["value"]) == ("stopped", 1, 0, None)
    assert "deadline" in r["reason"] and eng.statements_run == 0


@pytest.mark.parametrize("sdk", SDKS)
def test_past_the_deadline_the_next_model_call_does_not_start(es, eng, sdk):
    clock = Clock()
    handler = load("handler", "sdk_handler")
    toollog = load("toollog")
    real_call = toollog.SqlTool.__call__

    def slow_tool(self, sql):
        out = real_call(self, sql)
        clock.t += 30              # the query returned at 30 s; the second model call is about to start
        return out

    toollog.SqlTool.__call__ = slow_tool
    try:
        model = scripted(sdk, WHOLE_DAY)
        r = asyncio.run(handler.answer_async("How many trips?", es, sdk=sdk, model=model, eng=eng, clock=clock))
    finally:
        toollog.SqlTool.__call__ = real_call
    assert (r["status"], r["model_calls"], r["tool_calls"]) == ("stopped", 1, 1)
    assert calls_of(model) == 1


def test_the_function_as_lambda_has_it(tmp_path, trips, monkeypatch):
    """Only the package's files, the environment deploy.py gives the function, the token in Parameter Store."""
    import shutil
    import importlib.util
    import sys

    from moto import mock_aws

    deploy = load("deploy", "sdk_deploy")
    ext_env()
    s = load("settings").load_settings()
    pkg = tmp_path / "task"
    pkg.mkdir()
    for name in deploy.FUNCTION_FILES:
        shutil.copy(load("handler", "sdk_handler").HERE / name, pkg / name)
    shutil.copy(trips, pkg / "trips.parquet")
    for name in list(os.environ):
        if name in load("settings").ENV_NAMES.values() or name in ("COURSE_MCP_TOKEN", "GATEWAY_URL"):
            monkeypatch.delenv(name)
    for name, value in {**s.deployed_env(), "AWS_REGION": "ca-central-1"}.items():
        monkeypatch.setenv(name, value)
    saved = {m: sys.modules.pop(m) for m in list(sys.modules) if m in {n[:-3] for n in deploy.FUNCTION_FILES}}
    monkeypatch.syspath_prepend(str(pkg))
    try:
        with mock_aws():
            import boto3

            boto3.client("ssm", region_name="ca-central-1").put_parameter(
                Name=s.token_param, Value="token-from-ssm", Type="SecureString")
            spec = importlib.util.spec_from_file_location("lambda_handler", pkg / "handler.py")
            fn = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(fn)
            seen = {}

            def model_for(s_, sdk):
                seen["token"] = fn.token(s_)
                return scripted(sdk, WHOLE_DAY)

            fn.model_for = model_for
            out = fn.handler({"body": json.dumps({"question": "How many trips were requested on 2024-03-14?"})}, None)
    finally:
        for m in {n[:-3] for n in deploy.FUNCTION_FILES}:
            sys.modules.pop(m, None)
        sys.modules.update(saved)
    body = json.loads(out["body"])
    assert out["statusCode"] == 200 and (body["status"], body["value"]) == ("answered", 19074), body
    assert seen["token"] == "token-from-ssm"

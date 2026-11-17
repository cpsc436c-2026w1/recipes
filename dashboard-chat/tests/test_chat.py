import json

import httpx

from main.chat import MAX_MODEL_CALLS, answer
from main.model import GatewayModel, ModelReply, StubModel, ToolCall
from main.traces import LocalTraces


def test_number_comes_from_duckdb(engine, stub, plain_duckdb, version):
    a = answer("What were total sales in Vancouver?", user_id="c-0007", model=stub, engine=engine,
               data_version=version)
    expected = plain_duckdb.execute(
        "SELECT ROUND(SUM(o.amount_cad), 2) FROM orders o JOIN stores s USING (store_id) WHERE city = 'Vancouver'"
    ).fetchone()[0]
    assert a.status == "answered"
    assert a.rows[0][0] == float(expected)
    assert str(float(expected)) in a.reply
    assert [s.kind for s in a.steps] == ["model_call", "tool_call", "model_call"]
    assert a.data_version == version


def test_my_question_uses_the_askers_id(engine, stub, plain_duckdb):
    for user in ("c-0007", "c-0012"):
        a = answer("How much did I spend in total?", user_id=user, model=stub, engine=engine)
        expected = plain_duckdb.execute(
            "SELECT ROUND(SUM(amount_cad), 2) FROM orders WHERE customer_id = ?", [user]).fetchone()[0]
        assert a.rows[0][0] == float(expected)


def test_rejected_sql_goes_back_to_the_model(engine, stub):
    a = answer("Delete all the orders", user_id="c-0007", model=stub, engine=engine)
    assert a.status == "no_answer"
    assert a.steps[1].status == "rejected" and "DELETE" in a.steps[1].detail
    assert a.sql == ""


def test_off_topic_question_is_declined_without_a_query(engine, stub):
    a = answer("What is the weather today?", user_id="c-0007", model=stub, engine=engine)
    assert a.status == "no_answer" and len(a.steps) == 1


class LoopingModel:
    """A model that never stops calling the tool: the loop must end anyway."""
    model = "qwen3.6-35b-a3b"

    def chat(self, messages, tools=None):
        return ModelReply(None, [ToolCall("x", "run_sql", {"sql": "SELECT 1"})], 10, 2, self.model)


def test_loop_is_bounded(engine):
    a = answer("anything", user_id="c-0007", model=LoopingModel(), engine=engine)
    assert sum(s.kind == "model_call" for s in a.steps) == MAX_MODEL_CALLS
    assert sum(s.kind == "tool_call" for s in a.steps) == MAX_MODEL_CALLS - 1  # the last query is not run
    assert (a.status, a.sql, a.columns, a.rows) == ("no_answer", "", [], [])  # no table next to the reply
    assert "step limit" in a.reply


class FailingModel:
    model = "qwen3.6-35b-a3b"

    def chat(self, messages, tools=None):
        raise RuntimeError("course LLM gateway refused the call: HTTP 429 budget_exhausted")


def test_model_failure_is_an_error_answer_with_a_trace(engine, tmp_path):
    traces = LocalTraces(tmp_path / "t.jsonl")
    a = answer("How many orders are there?", user_id="c-0007", model=FailingModel(), engine=engine, traces=traces)
    assert a.status == "error" and "429" in a.error
    item = traces.all()[0]
    assert item["status"] == "error" and "user_id" not in item and len(item["user_hash"]) == 16


def test_gateway_request_shape():
    """The real client, against a fake transport: URL, bearer token, tools, and the parsed tool call."""
    seen = {}

    def handler(request: httpx.Request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "model": "qwen3.6-35b-a3b",
            "choices": [{"message": {"content": None, "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "run_sql",
                                                              "arguments": "{\"sql\": \"SELECT 1\"}"}}]}}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 9}})

    m = GatewayModel("https://gw.example/v1", "tok-123", "qwen3.6-35b-a3b")
    m.http = httpx.Client(base_url="https://gw.example/v1", transport=httpx.MockTransport(handler),
                          headers={"Authorization": "Bearer tok-123"})
    r = m.chat([{"role": "user", "content": "hi"}], tools=[{"type": "function", "function": {"name": "run_sql"}}])
    assert seen["url"] == "https://gw.example/v1/chat/completions"
    assert seen["auth"] == "Bearer tok-123"
    assert seen["body"]["model"] == "qwen3.6-35b-a3b" and seen["body"]["tool_choice"] == "auto"
    assert r.tool_calls[0].arguments == {"sql": "SELECT 1"} and r.tokens_in == 120


def test_stub_cost_uses_the_gateway_price_table(engine):
    a = answer("How many orders are there?", user_id="c-0007", model=StubModel(
        {"How many orders are there?": {"sql": "SELECT COUNT(*) FROM orders"}}), engine=engine)
    assert a.cost_cad > 0
    assert abs(a.cost_cad - (a.tokens_in * 0.05 + a.tokens_out * 0.10) / 1e6) < 1e-12


# ---- what a refusal by the gateway does: the user, the trace, the cache, the spans, the metrics ----

import pytest  # noqa: E402


def refusing_gateway(status: int) -> GatewayModel:
    def handler(request: httpx.Request):
        return httpx.Response(status, json={"error": {"message": "refused", "code": f"code-{status}"}})

    m = GatewayModel("https://gw.example/v1", "tok-123", "qwen3.6-35b-a3b")
    m.http = httpx.Client(base_url="https://gw.example/v1", transport=httpx.MockTransport(handler))
    return m


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_gateway_refusal_is_an_error_everywhere_and_a_success_nowhere(engine, tmp_path, status):
    from alarms import put_request_metrics
    from cache import LocalCache, cached_answer
    from spans import spans_of

    traces, cache = LocalTraces(tmp_path / "t.jsonl"), LocalCache()

    def ask(q):
        return answer(q, user_id="c-0007", model=refusing_gateway(status), engine=engine, traces=traces)

    d, hit = cached_answer("How many orders are there?", user_id="c-0007", cache=cache, data_version="v1", answer_version='test', ask=ask)
    assert (d["status"], hit, d["reply"], d["rows"], d["cost_cad"]) == ("error", False, "", [], 0.0)
    assert f"{status}" in d["error"] and "tok-123" not in d["error"]  # the reason, never the token
    assert cache.items == {}  # a failed call is not stored as an answer
    item = traces.all()[0]
    assert item["status"] == "error" and str(status) in item["error"]
    assert [(s["kind"], s["status"]) for s in item["steps"]] == [("model_call", "error")]
    root, call = spans_of(d)
    assert root["error"] is True and call["error"] is True
    sent = []
    put_request_metrics(d, "test/dashboard-chat", "ca-central-1",
                        client=type("CW", (), {"put_metric_data": lambda self, **kw: sent.append(kw)})())
    assert {m["MetricName"]: m["Value"] for m in sent[0]["MetricData"]}["Errors"] == 1


@pytest.mark.parametrize("code,retry_after,waits,sent,slept", [
    ("rate_limit_exceeded", "60", 0, 1, []),        # the chat: no wait, the refusal is the answer
    ("rate_limit_exceeded", "60", 1, 2, [60.0]),    # a batch run: one wait for Retry-After, then one more attempt
    ("upstream_rate_limited", "60", 1, 2, [60.0]),  # the model service's rate limit clears by waiting too
    ("rate_limit_exceeded", "120", 1, 2, [65]),     # never longer than MAX_RATE_LIMIT_WAIT_S
    ("rate_limit_exceeded", None, 1, 2, [5]),       # no Retry-After: DEFAULT_RATE_LIMIT_WAIT_S
    ("budget_exhausted", "60", 1, 1, []),           # a spent budget does not clear by waiting
])
def test_only_a_rate_limit_refusal_is_waited_out_and_only_when_asked(code, retry_after, waits, sent, slept):
    seen, naps = [], []

    def handler(request: httpx.Request):
        seen.append(request)
        headers = {"Retry-After": retry_after} if retry_after else {}
        return httpx.Response(429, headers=headers, json={"error": {"message": "refused", "code": code}})

    m = GatewayModel("https://gw.example/v1", "tok-123", "qwen3.6-35b-a3b", rate_limit_waits=waits, sleep=naps.append)
    m.http = httpx.Client(base_url="https://gw.example/v1", transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError, match="HTTP 429"):
        m.chat([{"role": "user", "content": "hi"}])
    assert (len(seen), naps) == (sent, slept)


class FailsOnSecondCall:
    """The first call writes a query; the gateway refuses the second (the rate limit of the run of 2026-11-12)."""
    model = "qwen3.6-35b-a3b"

    def __init__(self, sql):
        self.sql, self.calls = sql, 0

    def chat(self, messages, tools=None):
        self.calls += 1
        if self.calls == 1:
            return ModelReply(None, [ToolCall("c1", "run_sql", {"sql": self.sql})], 10, 2, self.model)
        raise RuntimeError("course LLM gateway refused the call: HTTP 429 rate_limit_exceeded")


def test_run_prints_the_reason_of_an_error_and_exits_1(engine, tmp_path, monkeypatch, capsys):
    import main.run as run

    traces = LocalTraces(tmp_path / "t.jsonl")
    monkeypatch.setattr(run, "build", lambda target, kind, settings: (
        engine, FailsOnSecondCall("SELECT COUNT(*) FROM orders"), traces, "v1"))
    with pytest.raises(SystemExit) as e:
        run.main(["--target", "local", "--model", "stub", "--question", "How many orders are there?"])
    line = json.loads(capsys.readouterr().out.splitlines()[0])
    assert line["status"] == "error" and "HTTP 429" in line["error"]
    assert "1 of 1 questions ended with status error" in str(e.value)
    assert traces.all()[0]["status"] == "error"


def test_run_without_errors_prints_no_error_field_and_exits_0(engine, stub, capsys, monkeypatch, tmp_path):
    import main.run as run

    monkeypatch.setattr(run, "build", lambda target, kind, settings: (
        engine, stub, LocalTraces(tmp_path / "t.jsonl"), "v1"))
    run.main(["--target", "local", "--model", "stub", "--question", "How many orders are there?"])
    assert "error" not in json.loads(capsys.readouterr().out.splitlines()[0])


def test_kb_mcp_token_wins_and_the_older_names_still_work(monkeypatch):
    for name in ("COURSE_GATEWAY_URL", "GATEWAY_URL", "KB_MCP_TOKEN", "COURSE_MCP_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GATEWAY_URL", "https://older.example/v1/")
    monkeypatch.setenv("COURSE_MCP_TOKEN", "older-token")
    from main import config

    s = config.Settings()
    assert (s.gateway_url, s.token) == ("https://older.example/v1", "older-token")
    monkeypatch.setenv("COURSE_GATEWAY_URL", "https://gateway.example/v1")
    monkeypatch.setenv("KB_MCP_TOKEN", "new-token")
    s = config.Settings()
    assert (s.gateway_url, s.token) == ("https://gateway.example/v1", "new-token")


def test_no_temperature_for_gpt_models():
    from main.model import sampling

    assert sampling("qwen3.6-35b-a3b") == {"temperature": 0}
    for model in ("gpt-4o", "openai/gpt-4.1", "o3"):
        assert sampling(model) == {}

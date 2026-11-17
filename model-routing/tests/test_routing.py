"""Validation of the main scenario: each label runs its fixed path; out-of-scope gets the stock reply without a
large-model call; misroutes are logged and go to the hard path. Offline: the stub model, no key."""

from __future__ import annotations

import json

import httpx
import pytest

from main.app import MisrouteLog, Models, handle
from main.config import Settings
from main.model import GatewayModel, StubModel, cost_cad
from main.paths import STOCK_REPLY
from main.router import ROUTER_SYSTEM, parse_label
from main.run import HERE, make_models

SMALL, LARGE = "qwen3.6-35b-a3b", "qwen3.8-27b"


@pytest.fixture
def models():
    return make_models("stub", Settings(small_model=SMALL, large_model=LARGE))


@pytest.fixture
def log(tmp_path):
    return MisrouteLog(tmp_path / "misroutes.jsonl")


def test_out_of_scope_gets_stock_reply_without_a_large_model_call(models, log):
    out = handle("What will the weather be in Whistler tomorrow?", models, log)
    assert out["label"] == out["path"] == "out_of_scope"
    assert out["reply"] == STOCK_REPLY
    assert out["large_model_calls"] == 0
    assert [c["part"] for c in out["calls"]] == ["router"]          # the router call only
    assert models.client.calls == [SMALL]


def test_lookup_reads_the_order_without_any_answer_model(models, log):
    out = handle("What is the status of order 1042?", models, log)
    assert out["path"] == "lookup" and "shipped" in out["reply"]
    assert models.client.calls == [SMALL]


def test_search_uses_the_small_model_and_hard_the_large(models, log):
    s = handle("Do you have waterproof jackets?", models, log)
    h = handle("I run trails in the rain and have 200 CAD. Which shoe and socks should I get, and why?", models, log)
    assert [c["model"] for c in s["calls"]] == [SMALL, SMALL]
    assert [c["model"] for c in h["calls"]] == [SMALL, LARGE]
    assert h["cost_cad"] > s["cost_cad"]


def test_lookup_without_a_number_is_logged_and_sent_to_hard(models, log):
    out = handle("Where is my order?", models, log)
    assert out["label"] == "lookup" and out["path"] == "hard"
    assert log.lines[-1]["reason"] == "lookup without an order number"
    lines = [json.loads(x) for x in log.path.read_text().splitlines()]
    assert lines[-1]["sent_to"] == "hard"


def test_unknown_router_reply_goes_to_hard_and_is_logged(models, log):
    out = handle("Can I return boots I wore once on a hike?", models, log)
    assert out["path"] == "hard" and out["large_model_calls"] == 1
    assert log.lines[-1]["reason"] == "router reply is not a label"


@pytest.mark.parametrize("reply,label,known", [
    ("lookup", "lookup", True), (" Search.\n", "search", True), ("out-of-scope", "out_of_scope", True),
    ("OUT_OF_SCOPE", "out_of_scope", True), ("", "hard", False), ("I think lookup", "hard", False),
])
def test_parse_label(reply, label, known):
    r = parse_label(reply)
    assert (r.label, r.known) == (label, known)


def test_cost_uses_the_gateway_price_table():
    assert cost_cad(LARGE, 1_000_000, 0) == pytest.approx(0.10)
    assert cost_cad(SMALL, 0, 1_000_000) == pytest.approx(0.10)


def test_gateway_client_sends_the_openai_request_and_reads_usage():
    """The real path, against a fake transport: no network, no key."""
    seen = {}

    def answer(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"model": SMALL, "choices": [{"message": {"content": "search"}}],
                                         "usage": {"prompt_tokens": 120, "completion_tokens": 1}})

    g = GatewayModel("https://gateway.example/v1", "token-for-tests")
    g.http = httpx.Client(base_url="https://gateway.example/v1", transport=httpx.MockTransport(answer),
                          headers={"Authorization": "Bearer token-for-tests"})
    r = g.chat(SMALL, ROUTER_SYSTEM, "Any headlamps?", 8)
    assert r.text == "search" and r.tokens_in == 120
    assert seen["auth"] == "Bearer token-for-tests"
    assert seen["body"]["model"] == SMALL and seen["body"]["max_tokens"] == 8
    assert seen["body"]["messages"][0] == {"role": "system", "content": ROUTER_SYSTEM}


def test_reply_is_priced_as_the_model_that_was_asked_for():
    """An endpoint that names the model differently in its reply: the cost and the large-model count still hold."""
    def answer(request: httpx.Request) -> httpx.Response:
        asked = json.loads(request.content)["model"]
        return httpx.Response(200, json={"model": f"provider/{asked.upper()}-0613",
                                         "choices": [{"message": {"content": "a reply"}}],
                                         "usage": {"prompt_tokens": 1_000_000, "completion_tokens": 0}})

    g = GatewayModel("https://gateway.example/v1", "token-for-tests")
    g.http = httpx.Client(base_url="https://gateway.example/v1", transport=httpx.MockTransport(answer))
    r = g.chat(LARGE, "s", "u", 8)
    assert r.model == LARGE
    assert r.cost_cad == pytest.approx(0.10)          # before: 0.0, the reply's name is not in the price table


@pytest.mark.parametrize("status", [401, 403, 429, 500, 502])
def test_every_gateway_error_is_raised_and_names_the_status(status):
    g = GatewayModel("https://gateway.example/v1", "token-for-tests")
    g.http = httpx.Client(base_url="https://gateway.example/v1", transport=httpx.MockTransport(
        lambda req: httpx.Response(status, json={"error": {"code": "x", "message": "refused"}})))
    with pytest.raises((RuntimeError, httpx.HTTPStatusError), match=str(status)):
        g.chat(SMALL, "s", "u", 8)


def test_gateway_refusal_is_raised_not_retried():
    calls = []
    g = GatewayModel("https://gateway.example/v1", "token-for-tests")
    g.http = httpx.Client(base_url="https://gateway.example/v1",
                          transport=httpx.MockTransport(lambda req: calls.append(1) or httpx.Response(429, json={})))
    with pytest.raises(RuntimeError, match="429"):
        g.chat(SMALL, "s", "u", 8)
    assert len(calls) == 1


def test_gateway_needs_url_and_token():
    with pytest.raises(SystemExit):
        GatewayModel("", "")


def test_main_requests_all_have_stub_labels():
    stub = json.loads((HERE / "stub_replies.json").read_text())["router"]
    texts = [t for t in (HERE / "requests.txt").read_text().splitlines() if t.strip()]
    assert set(texts) == set(stub)


def test_kb_mcp_token_wins_and_the_older_names_still_work(monkeypatch):
    for name in ("COURSE_GATEWAY_URL", "GATEWAY_URL", "KB_MCP_TOKEN", "COURSE_MCP_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GATEWAY_URL", "https://older.example/v1/")
    monkeypatch.setenv("COURSE_MCP_TOKEN", "older-token")
    s = Settings()
    assert (s.gateway_url, s.token) == ("https://older.example/v1", "older-token")
    monkeypatch.setenv("COURSE_GATEWAY_URL", "https://gateway.example/v1")
    monkeypatch.setenv("KB_MCP_TOKEN", "new-token")
    s = Settings()
    assert (s.gateway_url, s.token) == ("https://gateway.example/v1", "new-token")


def test_no_temperature_for_gpt_models():
    from main.model import sampling

    assert sampling("qwen3.6-35b-a3b") == {"temperature": 0}
    for model in ("gpt-4o", "openai/gpt-4.1", "o3"):
        assert sampling(model) == {}

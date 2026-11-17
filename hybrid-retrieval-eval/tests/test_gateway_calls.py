"""Calls to the course LLM gateway, against a local stand-in (httpx.MockTransport): how many requests an eval
sends, and what happens when the gateway refuses one. No network, no key."""

import json

import httpx
import pytest

import main.run
from main.config import load_products, load_queries
from main.embed import GatewayEmbedder
from main.evaluate import evaluate
from main.llm import MAX_TRIES, MAX_WAIT_S, GatewayChat, GatewayRefused
from main.search import LocalSearch


class FakeGateway:
    """Answers /embeddings and /chat/completions. `refusals` is a list of (status, code, retry_after) served first,
    one per request, before it starts to answer."""

    def __init__(self, refusals=()):
        self.refusals = list(refusals)
        self.requests: list[tuple[str, dict]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append((request.url.path, body))
        if self.refusals:
            status, code, retry_after = self.refusals.pop(0)
            headers = {"retry-after": str(retry_after)} if retry_after is not None else {}
            return httpx.Response(status, headers=headers, json={"error": {"code": code, "message": code}})
        if request.url.path.endswith("/embeddings"):
            data = [{"index": i, "embedding": [float(len(t)), 1.0, float(i % 3)]} for i, t in enumerate(body["input"])]
            return httpx.Response(200, json={"data": data[::-1], "usage": {"prompt_tokens": len(body["input"])}})
        return httpx.Response(200, json={"choices": [{"message": {"content": "Try [P01]."}}],
                                         "usage": {"prompt_tokens": 10, "completion_tokens": 3}})

    def client(self) -> httpx.Client:
        return httpx.Client(base_url="https://gw.example/v1", transport=httpx.MockTransport(self))

    def count(self, path: str) -> int:
        return sum(p.endswith(path) for p, _ in self.requests)


def embedder(gw, sleeps=None, **kw):
    return GatewayEmbedder("https://gw.example/v1", "tok", http=gw.client(),
                           sleep=(sleeps if sleeps is not None else []).append, **kw)


def test_texts_go_in_batches_and_each_text_once():
    gw = FakeGateway()
    e = embedder(gw)
    texts = [f"text {i}" for i in range(70)]
    v = e.embed(texts)
    assert v.shape == (70, 3) and gw.count("/embeddings") == 3          # 32 + 32 + 6
    assert [len(b["input"]) for _, b in gw.requests] == [32, 32, 6]
    again = e.embed(["text 5", "text 5", "text 69"])                    # asked before: no request
    assert gw.count("/embeddings") == 3
    assert (again[0] == v[5]).all() and (again[1] == v[5]).all() and (again[2] == v[69]).all()


def test_an_eval_sends_one_embedding_request_for_the_queries_not_one_per_search():
    gw = FakeGateway()
    e = embedder(gw)
    products, queries = load_products(), load_queries()
    search = LocalSearch(products, e)
    assert gw.count("/embeddings") == 1                                 # 30 products, one batch
    e.embed([q["query"] for q in queries])                              # what main/run.py does before the eval
    evaluate(search, queries)                                           # embedding and hybrid, for every query
    assert gw.count("/embeddings") == 2
    assert e.tokens_in == len(products) + len(queries)


def test_the_run_embeds_all_queries_in_one_request(monkeypatch, tmp_path, capsys):
    gw = FakeGateway()
    monkeypatch.setattr(main.run, "GatewayEmbedder", lambda url, token: embedder(gw))
    monkeypatch.setattr(main.run, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(main.run, "ROOT", tmp_path)
    main.run.main(["--embedder", "gateway"])
    assert "Retrieval (16 labelled queries, local indexes, gateway embeddings)" in capsys.readouterr().out
    assert gw.count("/embeddings") == 2                                 # the products, then the queries


def test_a_rate_limit_refusal_is_waited_out_for_as_long_as_retry_after_says():
    gw = FakeGateway([(429, "rate_limit_exceeded", 7), (429, "rate_limit_exceeded", None)])
    sleeps = []
    v = embedder(gw, sleeps).embed(["a", "b"])
    assert v.shape == (2, 3) and gw.count("/embeddings") == 3
    assert sleeps == [7.0, 2.0]                                         # Retry-After, then backoff: 2 s on try 2


def test_retries_are_bounded():
    gw = FakeGateway([(429, "rate_limit_exceeded", 1)] * 50)
    sleeps = []
    with pytest.raises(GatewayRefused) as refused:
        embedder(gw, sleeps).embed(["a"])
    assert refused.value.status == 429 and gw.count("/embeddings") == MAX_TRIES
    assert sleeps == [1.0] * (MAX_TRIES - 1)


@pytest.mark.parametrize("status, code, retry_after", [
    (429, "budget_exhausted", 30),            # waiting does not bring a budget back
    (401, "invalid_api_key", None),
    (429, "rate_limit_exceeded", MAX_WAIT_S + 1),   # too long to sit through: stop and say so
])
def test_other_refusals_stop_at_once(status, code, retry_after):
    gw = FakeGateway([(status, code, retry_after)] * 5)
    sleeps = []
    with pytest.raises(GatewayRefused) as refused:
        embedder(gw, sleeps).embed(["a"])
    assert (refused.value.status, refused.value.code) == (status, code)
    assert gw.count("/embeddings") == 1 and sleeps == []


def test_chat_waits_out_a_rate_limit_too():
    gw = FakeGateway([(429, "rate_limit_exceeded", 3)])
    sleeps = []
    chat = GatewayChat("https://gw.example/v1", "tok", http=gw.client(), sleep=sleeps.append)
    reply = chat.chat("qwen3.6-35b-a3b", "system", "user")
    assert reply.text == "Try [P01]." and sleeps == [3.0] and gw.count("/chat/completions") == 2


def test_a_reply_with_too_few_embeddings_is_an_error_not_a_shifted_index():
    def short(request):
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0, 0.0]}]})

    http = httpx.Client(base_url="https://gw.example/v1", transport=httpx.MockTransport(short))
    with pytest.raises(RuntimeError, match="1 embeddings for 2 texts"):
        GatewayEmbedder("https://gw.example/v1", "tok", http=http).embed(["a", "b"])


def test_retry_after_as_an_http_date_is_read_as_seconds():
    """The gateway forwards the provider's Retry-After, which may be a date (RFC 9110, section 10.2.3)."""
    from datetime import datetime, timezone

    from main.llm import _retry_after

    now = datetime(2026, 11, 12, 22, 0, 0, tzinfo=timezone.utc)
    assert _retry_after("Thu, 12 Nov 2026 22:00:07 GMT", default=1.0, now=now) == 7.0
    assert _retry_after("Thu, 12 Nov 2026 21:59:00 GMT", default=1.0, now=now) == 0.0   # already past
    assert _retry_after("3", default=1.0) == 3.0 and _retry_after("soon", default=1.0) == 1.0
    assert _retry_after(None, default=2.0) == 2.0


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
    from main.llm import sampling

    assert sampling("qwen3.6-35b-a3b") == {"temperature": 0}
    for model in ("gpt-4o", "openai/gpt-4.1", "o3"):
        assert sampling(model) == {}

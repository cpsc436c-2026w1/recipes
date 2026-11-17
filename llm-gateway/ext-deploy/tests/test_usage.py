import socket

import httpx
import pytest

import fake_upstream
from conftest import bearer, chat_body, key_item, usage_rows, wait_for_usage
from gateway import pricing, upstream


def test_pricing_integer_micro_units():
    assert pricing.cost("example-large", 1_000_000, 1_000_000) == 300_000
    assert pricing.cost("example-small", 1_000_000, 0) == 50_000
    assert pricing.cost_in("example-large", 1) == 1  # rounded up
    assert pricing.cost_in("example-embed", 0) == 0
    assert pricing.micro_to_amount(3_000_000) == "3.000000"


def test_non_streaming_recorded_once(client, issue):
    key_id, key = issue()
    r = client.post("/v1/chat/completions", json=chat_body(max_tokens=50), headers=bearer(key))
    assert r.status_code == 200
    assert r.headers["x-request-id"]
    usage = r.json()["usage"]
    cost = pricing.cost("example-large", usage["prompt_tokens"], usage["completion_tokens"])
    [row] = usage_rows(key_id)
    assert row["requests"] == 1
    assert row["tokens_in"] == usage["prompt_tokens"]
    assert row["tokens_out"] == usage["completion_tokens"]
    assert row["cost_micro"] == cost
    item = key_item(key_id)
    assert item["spent_micro"] == cost  # the reserve was settled to the real cost
    assert item["requests"] == 1


def test_buffered_reply_names_its_charge_and_a_stream_does_not(client, issue):
    # The header reports what the settle wrote; it changes nothing in what the key is charged.
    key_id, key = issue()
    r = client.post("/v1/chat/completions", json=chat_body(max_tokens=50), headers=bearer(key))
    usage = r.json()["usage"]
    cost = pricing.cost("example-large", usage["prompt_tokens"], usage["completion_tokens"])
    assert r.headers["x-gateway-cost-micro"] == str(cost) == str(key_item(key_id)["spent_micro"])
    s = client.post("/v1/chat/completions", json=chat_body(max_tokens=50, stream=True), headers=bearer(key))
    assert s.status_code == 200 and "x-gateway-cost-micro" not in s.headers
    n = client.post("/v1/chat/completions", json=chat_body(max_tokens=50, x_fake={"no_usage": True}),
                    headers=bearer(key))
    assert n.status_code == 200 and "x-gateway-cost-micro" not in n.headers  # settled at the reserve, an estimate


def test_streaming_recorded_once_with_include_usage(client, issue):
    key_id, key = issue()
    with client.stream("POST", "/v1/chat/completions", json=chat_body(stream=True, x_fake={"chunks": 4}),
                       headers=bearer(key)) as r:
        r.read()
    assert fake_upstream.received[-1]["body"]["stream_options"] == {"include_usage": True}
    [row] = wait_for_usage(key_id)
    assert row["requests"] == 1
    assert row["tokens_in"] == 18 and row["tokens_out"] == 4
    assert "requests_estimated" not in row
    assert key_item(key_id)["spent_micro"] == pricing.cost("example-large", 18, 4)


def test_streaming_without_usage_is_estimated(client, issue):
    key_id, key = issue()
    with client.stream("POST", "/v1/chat/completions",
                       json=chat_body(stream=True, x_fake={"chunks": 5, "no_usage": True}),
                       headers=bearer(key)) as r:
        r.read()
    [row] = wait_for_usage(key_id)
    assert row["requests_estimated"] == 1
    assert row["tokens_out"] == 5  # one per content chunk
    assert row["tokens_in"] == 18  # the reserve's input estimate


def test_embeddings_counted(client, issue):
    key_id, key = issue()
    r = client.post("/v1/embeddings", json={"model": "example-embed", "input": ["abc", "defg"]},
                    headers=bearer(key))
    assert r.status_code == 200
    [row] = usage_rows(key_id)
    assert row["sk"].endswith("#example-embed")
    assert row["tokens_in"] == 7 and row["tokens_out"] == 0
    assert row["cost_micro"] == pricing.cost_in("example-embed", 7)


def _reserve(max_tokens: int) -> int:
    return pricing.cost_in("example-large", 18) + pricing.cost_out("example-large", max_tokens)  # "hi" + 16


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("status", [500, 502, 504])
def test_upstream_5xx_keeps_the_whole_reserve(client, issue, stream, status):
    # The upstream had the request: the provider may have generated up to max_tokens and may bill them.
    key_id, key = issue()
    r = client.post("/v1/chat/completions", json=chat_body(stream=stream, max_tokens=100, x_fake={"status": status}),
                    headers=bearer(key))
    assert r.status_code == 502
    assert r.json() == {"error": {"message": "upstream failed", "type": "upstream_error", "code": "upstream_error"}}
    [row] = usage_rows(key_id)
    assert row["requests_failed"] == 1 and row["requests_estimated"] == 1
    assert row["cost_micro"] == _reserve(100) == key_item(key_id)["spent_micro"]
    assert row["tokens_in"] == 18 and row["tokens_out"] == 100


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("status", [503, 529])
def test_upstream_unavailable_or_overloaded_gives_the_reserve_back(client, issue, stream, status):
    # The provider turned the request away (unavailable, overloaded): it generated nothing.
    key_id, key = issue()
    r = client.post("/v1/chat/completions", json=chat_body(stream=stream, max_tokens=100, x_fake={"status": status}),
                    headers=bearer(key))
    [row] = usage_rows(key_id)
    assert row["requests_failed"] == 1 and row["cost_micro"] == 0
    assert key_item(key_id)["spent_micro"] == 0


@pytest.mark.parametrize("status", [400, 401, 404])
def test_upstream_4xx_gives_the_reserve_back(client, issue, status):
    # The upstream refused the request: it did not generate anything.
    key_id, key = issue()
    r = client.post("/v1/chat/completions", json=chat_body(x_fake={"status": status}), headers=bearer(key))
    assert r.status_code == (502 if status == 401 else 400)  # 401: the gateway's own provider key
    [row] = usage_rows(key_id)
    assert row["requests_failed"] == 1 and row["cost_micro"] == 0
    assert key_item(key_id)["spent_micro"] == 0


def test_upstream_read_timeout_keeps_the_whole_reserve(client, issue, monkeypatch):
    # Sent, then no reply within the read timeout (60 s by default): the upstream may still be generating.
    monkeypatch.setenv("UPSTREAM_READ_TIMEOUT_S", "0.2")
    key_id, key = issue()
    r = client.post("/v1/chat/completions", json=chat_body(max_tokens=100, x_fake={"delay": 1.0}), headers=bearer(key))
    assert r.status_code == 502
    [row] = usage_rows(key_id)
    assert row["requests_failed"] == 1
    assert row["cost_micro"] == _reserve(100) == key_item(key_id)["spent_micro"]


def test_upstream_total_timeout_keeps_the_whole_reserve(client, issue, monkeypatch):
    monkeypatch.setenv("UPSTREAM_TOTAL_TIMEOUT_S", "0.2")
    key_id, key = issue()
    r = client.post("/v1/chat/completions", json=chat_body(max_tokens=100, x_fake={"delay": 1.0}), headers=bearer(key))
    assert r.status_code == 502
    assert key_item(key_id)["spent_micro"] == _reserve(100)


@pytest.mark.parametrize("stream", [False, True])
def test_connection_refused_gives_the_reserve_back(client, issue, monkeypatch, stream):
    # Nothing listens on the port: the request never reached an upstream.
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    monkeypatch.setenv("UPSTREAM_BASE_URL", f"http://127.0.0.1:{port}/v1")
    key_id, key = issue()
    r = client.post("/v1/chat/completions", json=chat_body(stream=stream), headers=bearer(key))
    assert r.status_code == 502
    [row] = usage_rows(key_id)
    assert row["requests_failed"] == 1 and row["cost_micro"] == 0
    assert key_item(key_id)["spent_micro"] == 0


def test_was_sent_tells_a_failure_before_sending_from_one_after():
    req = httpx.Request("POST", "http://upstream.invalid/v1/chat/completions")
    for err in (httpx.ConnectError("refused", request=req), httpx.ConnectTimeout("t", request=req),
                httpx.PoolTimeout("t", request=req)):
        assert upstream.was_sent(err) is False
    for err in (httpx.ReadTimeout("t", request=req), httpx.RemoteProtocolError("closed", request=req),
                httpx.ReadError("reset", request=req), TimeoutError()):
        assert upstream.was_sent(err) is True


def test_upstream_429_gets_gateway_body(client, issue):
    _, key = issue()
    r = client.post("/v1/chat/completions", json=chat_body(stream=True, x_fake={"status": 429}),
                    headers=bearer(key))
    assert r.status_code == 429
    assert "do not leak" not in r.text
    assert r.json()["error"]["code"] == "upstream_rate_limited"
    assert "retry-after" not in r.headers


def test_upstream_429_keeps_retry_after(client, issue):
    """The upstream's Retry-After reaches the caller, buffered and streamed."""
    _, key = issue()
    for stream in (False, True):
        r = client.post("/v1/chat/completions",
                        json=chat_body(stream=stream, x_fake={"status": 429, "retry_after": 7}), headers=bearer(key))
        assert r.status_code == 429
        assert r.headers["retry-after"] == "7"
        assert r.json()["error"]["code"] == "upstream_rate_limited"


def test_no_upstream_url_gives_502_and_the_reserve_back(client, issue, monkeypatch):
    monkeypatch.delenv("UPSTREAM_BASE_URL")
    key_id, key = issue()
    for stream in (False, True):
        r = client.post("/v1/chat/completions", json=chat_body(stream=stream), headers=bearer(key))
        assert r.status_code == 502
        assert r.json()["error"]["code"] == "upstream_error"
    assert key_item(key_id)["spent_micro"] == 0


def test_wrong_endpoint_for_model(client, issue):
    _, key = issue()
    r = client.post("/v1/embeddings", json={"model": "example-large", "input": "x"}, headers=bearer(key))
    assert r.status_code == 400


def test_no_charge_is_named_when_the_settle_fails(client, issue, monkeypatch):
    # The header says what the settle wrote. When the settle fails, nothing was written: the reply still goes out
    # (bookkeeping never breaks a reply), and it names no charge.
    from gateway import budget

    def broken(*args, **kwargs):
        raise RuntimeError("the usage table did not answer")

    key_id, key = issue()
    monkeypatch.setattr(budget, "settle", broken)
    r = client.post("/v1/chat/completions", json=chat_body(max_tokens=50), headers=bearer(key))
    assert r.status_code == 200 and r.json()["usage"]["completion_tokens"] > 0
    assert "x-gateway-cost-micro" not in r.headers

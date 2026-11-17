"""How an upstream error status reaches the caller: a 4xx the provider sent about this request is 400
`upstream_rejected` with the provider's message; the gateway's own provider key refused is 502; a 5xx is 502."""

import pytest

from conftest import bearer, chat_body, key_item, usage_rows
from gateway import upstream

CHAT = "/v1/chat/completions"


@pytest.mark.parametrize("stream", [False, True], ids=["buffered", "stream"])
def test_upstream_400_is_a_400_with_the_providers_message_and_no_charge(client, issue, stream):
    key_id, key = issue(owner="rejected")
    msg = "Unsupported value: 'temperature' does not support 0 with this model. Only the default (1) value is supported."
    r = client.post(CHAT, json=chat_body(stream=stream, x_fake={"status": 400, "message": msg}), headers=bearer(key))
    assert r.status_code == 400
    assert r.json() == {"error": {"message": msg, "type": "invalid_request_error", "code": "upstream_rejected"}}
    [row] = usage_rows(key_id)
    assert row["cost_micro"] == 0 and row["requests_failed"] == 1
    assert key_item(key_id)["spent_micro"] == 0


def test_a_long_provider_message_is_cut_and_a_body_without_one_gets_a_plain_sentence(client, issue):
    _, key = issue(owner="rejected-long")
    r = client.post(CHAT, json=chat_body(x_fake={"status": 422, "message": "x" * 1000}), headers=bearer(key))
    assert r.status_code == 400 and r.json()["error"]["message"] == "x" * upstream.REJECTED_MESSAGE_CHARS
    r = client.post(CHAT, json=chat_body(x_fake={"status": 400, "raw_error": "<html>bad</html>"}), headers=bearer(key))
    assert r.status_code == 400
    assert r.json()["error"]["message"] == "the model service rejected the request (HTTP 400)"


@pytest.mark.parametrize("stream", [False, True], ids=["buffered", "stream"])
def test_upstream_500_is_still_a_502(client, issue, stream):
    _, key = issue(owner="upstream-500")
    r = client.post(CHAT, json=chat_body(stream=stream, x_fake={"status": 500}), headers=bearer(key))
    assert r.status_code == 502 and r.json() == upstream.UPSTREAM_FAILED
    assert "do not leak" not in r.text


@pytest.mark.parametrize("status", [401, 403])
def test_upstream_401_or_403_is_a_502_with_a_plain_message(client, issue, caplog, status):
    key_id, key = issue(owner=f"upstream-{status}")
    r = client.post(CHAT, json=chat_body(x_fake={"status": status}), headers=bearer(key))
    assert r.status_code == 502 and r.json() == upstream.UPSTREAM_KEY_REFUSED
    assert "do not leak" not in r.text
    assert f"upstream refused the provider key request_id={r.headers['x-request-id']} key_id={key_id}" in caplog.text
    assert key_item(key_id)["spent_micro"] == 0


def test_the_read_timeout_default_waits_for_a_thinking_model(monkeypatch):
    from gateway import settings
    monkeypatch.delenv("UPSTREAM_READ_TIMEOUT_S", raising=False)
    s = settings.load()
    assert s.read_timeout_s == 240 and s.read_timeout_s < s.total_timeout_s

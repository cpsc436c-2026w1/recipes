import logging

import fake_upstream
from conftest import FAKE_UPSTREAM_KEY, bearer, chat_body
from gateway import secrets


def test_upstream_key_goes_only_upstream_never_back_or_logged(client, issue, caplog):
    caplog.set_level(logging.DEBUG)
    key_id, key = issue()
    replies = [
        client.post("/v1/chat/completions", json=chat_body(), headers=bearer(key)),
        client.post("/v1/chat/completions", json=chat_body(x_fake={"status": 500}), headers=bearer(key)),
        client.post("/v1/chat/completions", json=chat_body(n=3), headers=bearer(key)),
        client.post("/v1/embeddings", json={"model": "example-embed", "input": "a"}, headers=bearer(key)),
    ]
    with client.stream("POST", "/v1/chat/completions", json=chat_body(stream=True), headers=bearer(key)) as r:
        replies.append(r)
        r.read()
    assert fake_upstream.received[-1]["authorization"] == f"Bearer {FAKE_UPSTREAM_KEY}"
    for r in replies:
        assert FAKE_UPSTREAM_KEY not in r.text
        assert FAKE_UPSTREAM_KEY not in str(r.headers)
    logged = "\n".join(rec.getMessage() for rec in caplog.records)
    assert key_id in logged  # log lines carry the key id
    assert FAKE_UPSTREAM_KEY not in logged
    assert key not in logged  # and never the caller's key


def test_one_secret_read_per_container(client, issue):
    secrets.reset_for_tests()
    _, key = issue()
    for _ in range(3):
        assert client.post("/v1/chat/completions", json=chat_body(), headers=bearer(key)).status_code == 200
    assert secrets.reads == 1


def test_upstream_401_refreshes_secret(client, issue):
    secrets.reset_for_tests()
    _, key = issue()
    r = client.post("/v1/chat/completions", json=chat_body(x_fake={"status": 401}), headers=bearer(key))
    assert r.status_code == 502
    client.post("/v1/chat/completions", json=chat_body(), headers=bearer(key))
    assert secrets.reads == 2

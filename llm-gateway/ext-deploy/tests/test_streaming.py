import time

import httpx

from conftest import bearer, chat_body, wait_for_usage
from fake_upstream import render_stream


def test_chunks_pass_through_unchanged(client, issue):
    _, key = issue()
    with client.stream("POST", "/v1/chat/completions", json=chat_body(stream=True, x_fake={"chunks": 6}),
                       headers=bearer(key)) as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        got = b"".join(r.iter_raw())
    assert got == render_stream(6, (18, 6))


def test_first_chunk_before_upstream_finishes(client, issue):
    _, key = issue()
    start = time.monotonic()
    body = chat_body(stream=True, x_fake={"chunks": 5, "delay": 0.4})
    with client.stream("POST", "/v1/chat/completions", json=body, headers=bearer(key)) as r:
        it = r.iter_raw()
        first = next(it)
        t_first = time.monotonic() - start
        b"".join(it)
        t_all = time.monotonic() - start
    assert first.startswith(b"data: ")
    assert t_first < 1.0 < t_all


def test_disconnect_keeps_reading_and_records_real_usage(gateway_url, issue):
    key_id, key = issue()
    body = chat_body(stream=True, x_fake={"chunks": 6, "delay": 0.05})
    with httpx.Client(timeout=30) as c:
        with c.stream("POST", gateway_url + "/v1/chat/completions", json=body, headers=bearer(key)) as r:
            next(r.iter_raw())  # read one chunk, then hang up
    [row] = wait_for_usage(key_id)
    assert row["requests"] == 1
    assert row["tokens_out"] == 6
    assert "requests_estimated" not in row


def test_disconnect_with_slow_upstream_records_estimate(gateway_url, issue):
    key_id, key = issue()
    body = chat_body(stream=True, x_fake={"chunks": 40, "delay": 0.2})  # 8 s upstream, grace 0.5 s in tests
    start = time.monotonic()
    with httpx.Client(timeout=30) as c:
        with c.stream("POST", gateway_url + "/v1/chat/completions", json=body, headers=bearer(key)) as r:
            next(r.iter_raw())
    [row] = wait_for_usage(key_id)
    assert time.monotonic() - start < 4
    assert row["requests"] == 1
    assert row["requests_estimated"] == 1


def test_upstream_breaks_mid_stream(client, issue):
    key_id, key = issue()
    body = chat_body(stream=True, x_fake={"chunks": 5, "fail_after": 2})
    with client.stream("POST", "/v1/chat/completions", json=body, headers=bearer(key)) as r:
        assert r.status_code == 200
        text = r.read().decode()
    assert "upstream stream ended early" in text
    assert text.rstrip().endswith("data: [DONE]")
    [row] = wait_for_usage(key_id)
    assert row["requests_estimated"] == 1
    assert row["tokens_out"] == 2

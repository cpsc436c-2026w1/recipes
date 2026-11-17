"""The optional text log: one item per forwarded request, keyed by the key id and the request id."""

import json
import logging
import time

from conftest import bearer, chat_body, run_admin, text_row, text_rows
from gateway import aws, settings, textlog
from gateway.upstream import SSEMeter


def lines(caplog) -> list[str]:
    return [rec.getMessage() for rec in caplog.records if rec.name == "gateway"]


def test_buffered_request_stores_the_request_and_the_reply(client, issue):
    key_id, key = issue(team="t1")
    r = client.post("/v1/chat/completions", json=chat_body(), headers=bearer(key))
    assert r.status_code == 200
    row = text_row(key_id, r.headers["x-request-id"])
    assert (row["request_id"], row["team"], row["source"]) == (r.headers["x-request-id"], "t1", "gateway")
    assert (row["endpoint"], row["model"], row["status"]) == ("/chat/completions", "example-large", 200)
    sent = json.loads(row["request"])
    assert sent["messages"] == [{"role": "user", "content": "hi"}]
    assert sent["max_tokens"] == settings.DEFAULT_MAX_TOKENS  # the body as it went upstream
    assert json.loads(row["reply"]) == {"content": "bonjour", "finish_reason": "stop"}
    assert "truncated" not in row
    assert 179 * 86_400 < int(row["ttl"]) - time.time() <= 180 * 86_400


def test_streamed_reply_is_put_back_together(client, issue):
    key_id, key = issue()
    with client.stream("POST", "/v1/chat/completions", json=chat_body(stream=True, x_fake={"chunks": 4}),
                       headers=bearer(key)) as r:
        r.read()
    row = text_row(key_id, r.headers["x-request-id"])
    assert json.loads(row["reply"]) == {"content": "mot0 é mot1 é mot2 é mot3 é ", "finish_reason": None}
    assert row["team"] == "-"  # a key without a team


def test_non_ascii_text_is_stored_as_written(client, issue):
    key_id, key = issue()
    body = chat_body(messages=[{"role": "user", "content": "où est la gare, 駅"}])
    r = client.post("/v1/chat/completions", json=body, headers=bearer(key))
    assert "où est la gare, 駅" in text_row(key_id, r.headers["x-request-id"])["request"]


def test_failed_upstream_stores_the_request_with_its_status_and_no_reply(client, issue):
    key_id, key = issue()
    r = client.post("/v1/chat/completions", json=chat_body(x_fake={"status": 500}), headers=bearer(key))
    assert r.status_code == 502
    row = text_row(key_id, r.headers["x-request-id"])
    assert row["status"] == 500 and "reply" not in row
    assert "do not leak" not in json.dumps(row, default=str)


def test_embeddings_store_the_input_and_no_vectors(client, issue):
    key_id, key = issue()
    r = client.post("/v1/embeddings", json={"model": "example-embed", "input": ["premier", "second"]},
                    headers=bearer(key))
    assert r.status_code == 200
    row = text_row(key_id, r.headers["x-request-id"])
    assert row["endpoint"] == "/embeddings" and json.loads(row["request"])["input"] == ["premier", "second"]
    assert "reply" not in row


def test_refused_request_stores_nothing(client, issue):
    key_id, key = issue()
    assert client.post("/v1/chat/completions", json=chat_body(n=3), headers=bearer(key)).status_code == 400
    assert text_rows(key_id) == []


def test_off_without_a_table_name(client, issue, monkeypatch):
    monkeypatch.delenv("TABLE_TEXTS")
    key_id, key = issue()
    assert client.post("/v1/chat/completions", json=chat_body(), headers=bearer(key)).status_code == 200
    monkeypatch.setenv("TABLE_TEXTS", "llm-gateway-texts")
    assert text_rows(key_id) == []


def test_ttl_days_zero_stores_no_expiry(client, issue, monkeypatch):
    monkeypatch.setenv("TEXT_TTL_DAYS", "0")
    key_id, key = issue()
    r = client.post("/v1/chat/completions", json=chat_body(), headers=bearer(key))
    assert "ttl" not in text_row(key_id, r.headers["x-request-id"])


def test_a_failed_write_is_logged_and_the_reply_is_whole(client, issue, monkeypatch, caplog):
    caplog.set_level(logging.INFO)

    def no_table():
        raise RuntimeError("the table is gone")

    monkeypatch.setattr(aws, "texts_table", no_table)
    key_id, key = issue()
    body = chat_body(messages=[{"role": "user", "content": "un secret de test"}])
    r = client.post("/v1/chat/completions", json=body, headers=bearer(key))
    assert r.status_code == 200 and r.json()["choices"][0]["message"]["content"] == "bonjour"
    rid = r.headers["x-request-id"]
    logged = lines(caplog)
    assert any(x.startswith(f"text log failed request_id={rid} key_id={key_id} error=") for x in logged)
    assert any(x.startswith(f"request_id={rid} ") and " status=200 " in x for x in logged)  # settled all the same
    assert "un secret de test" not in "\n".join(logged)


def test_log_lines_never_carry_the_text(client, issue, caplog):
    caplog.set_level(logging.INFO)
    _, key = issue()
    body = chat_body(messages=[{"role": "user", "content": "phrase que personne ne doit lire"}])
    assert client.post("/v1/chat/completions", json=body, headers=bearer(key)).status_code == 200
    logged = "\n".join(lines(caplog))
    assert "phrase que personne" not in logged and "bonjour" not in logged


def test_a_request_over_the_item_limit_keeps_its_start_and_its_end(topic_arn, monkeypatch):
    monkeypatch.setattr(settings, "MAX_TEXT_ITEM_BYTES", 6_000)
    body = {"model": "example-large", "messages": [{"role": "user", "content": "début " + "é" * 10_000 + " fin"}]}
    textlog.write("rid-cut", {"key_id": "gw-CUTCUTCU", "team": "t9"}, "example-large", "/chat/completions", 200,
                  body, {"content": "ok", "finish_reason": "stop"})
    row = text_row("gw-CUTCUTCU", "rid-cut")
    assert row["truncated"] == len(json.dumps(body, ensure_ascii=False).encode())
    assert len(row["request"].encode()) + len(row["reply"].encode()) <= 6_000 + 10
    assert "début" in row["request"] and row["request"].endswith(' fin"}]}') and "\n...\n" in row["request"]


def test_reply_from_body_keeps_reasoning_and_tool_calls():
    call = {"id": "call_1", "type": "function", "function": {"name": "lookup", "arguments": '{"q": "x"}'}}
    data = {"choices": [{"message": {"role": "assistant", "content": None, "reasoning_content": "think",
                                     "tool_calls": [call]}, "finish_reason": "tool_calls"}]}
    assert textlog.reply_from_body(data) == {"reasoning_content": "think", "tool_calls": [call],
                                             "finish_reason": "tool_calls"}
    for other in (None, [], {"data": []}, {"choices": []}, {"choices": ["x"]}):
        assert textlog.reply_from_body(other) is None


def sse(delta: dict, finish=None) -> bytes:
    return b"data: " + json.dumps({"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}).encode() + b"\n\n"


def test_stream_meter_puts_tool_calls_and_reasoning_together():
    m = SSEMeter(keep_text=True)
    m.feed(sse({"role": "assistant", "reasoning_content": "je "}))
    m.feed(sse({"reasoning_content": "pense"}))
    m.feed(sse({"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                "function": {"name": "lookup", "arguments": '{"q": '}}]}))
    m.feed(sse({"tool_calls": [{"index": 1, "id": "call_2", "function": {"name": "other", "arguments": "{}"}}]}))
    m.feed(sse({"tool_calls": [{"index": 0, "function": {"arguments": '"x"}'}}]}))
    m.feed(sse({}, finish="tool_calls"))
    assert m.reply() == {
        "reasoning_content": "je pense",
        "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "lookup", "arguments": '{"q": "x"}'}},
                       {"id": "call_2", "type": "function", "function": {"name": "other", "arguments": "{}"}}],
        "finish_reason": "tool_calls"}
    assert m.content_chunks == 0


def test_stream_meter_keeps_no_text_unless_asked():
    m = SSEMeter()
    m.feed(sse({"content": "bonjour"}, finish="stop"))
    assert m.content_chunks == 1 and m.reply() is None


def test_admin_texts_writes_a_file_and_prints_only_counts(client, issue, tmp_path):
    key_id, key = issue(team="t-texts")
    other_id, other = issue(owner="bo")
    body = chat_body(messages=[{"role": "user", "content": "texte à relire"}])
    rid = client.post("/v1/chat/completions", json=body, headers=bearer(key)).headers["x-request-id"]
    client.post("/v1/chat/completions", json=chat_body(), headers=bearer(key))
    client.post("/v1/chat/completions", json=chat_body(), headers=bearer(other))

    out = tmp_path / "texts.jsonl"
    printed = run_admin("texts", str(out), "--team", "t-texts")
    assert printed.startswith(f"2 rows of 1 keys written to {out} (2 gateway)")
    assert "texte à relire" not in printed and "bonjour" not in printed
    assert out.stat().st_mode & 0o777 == 0o600
    rows = [json.loads(x) for x in out.read_text().splitlines()]
    assert {r["key_id"] for r in rows} == {key_id} and rows[0]["status"] == 200
    # Two requests in the same second have the same ts, so pick the row by its request id, not by its place.
    first = next(r for r in rows if r["request_id"] == rid)
    assert "texte à relire" in first["request"] and rows[0]["ts"] <= rows[1]["ts"]

    assert run_admin("texts", str(out), "--key", key_id, "--request", rid).startswith("1 rows of 1 keys")
    assert json.loads(out.read_text())["request_id"] == rid
    assert run_admin("texts", str(out), "--key", other_id, "--since", "2999-01-01").startswith("0 rows of 0 keys")
    assert run_admin("texts", str(out), "--request", rid).startswith("1 rows of 1 keys")  # a scan of every key

import json
from pathlib import Path
from types import SimpleNamespace as NS

import boto3
import pytest

import genai
import handler
import lake_example
import operational
from skeleton.events import distinct_in_window, generate, iso
from skeleton.lake_local import query
from skeleton.number import window
from stub import StubClient


@pytest.fixture
def table(aws, local_root, as_of):
    """The operational table, filled offline. Returns the tool, the expected number and the local lake folder."""
    events = generate(150, now=as_of, duplicates=5)
    operational.run_local(events, local_root, as_of, table="g-updates")
    handler._client = None
    ddb = boto3.client("dynamodb", region_name="ca-central-1")
    tool = lambda s, e: genai.count_updates(ddb, "g-updates", s, e, sleep=lambda _: None)   # noqa: E731
    return tool, distinct_in_window(events, *window(as_of)), ddb


def test_stub_reply_states_the_number_from_the_table(table, as_of):
    tool, expected, _ = table
    start, end = window(as_of)
    client = StubClient(start, end)
    answer = genai.ask("How many updates arrived in the last hour?", client, "stub", tool, start, end)
    assert answer["steps"] == 2
    assert answer["results"][0]["updates"] == expected
    assert str(expected) in answer["reply"] and genai.grounded(answer)
    sent = client.calls[0]
    assert sent["tools"] == genai.TOOLS and sent["messages"][0]["role"] == "system"
    assert iso(start) in sent["messages"][0]["content"]          # the window reaches the model


def test_the_tool_uses_only_batch_get_item(table, as_of, monkeypatch):
    _, expected, ddb = table
    used = []
    real = ddb._make_api_call
    monkeypatch.setattr(ddb, "_make_api_call", lambda op, kw: (used.append(op), real(op, kw))[1])
    start, end = window(as_of)
    assert genai.count_updates(ddb, "g-updates", iso(start), iso(end))["updates"] == expected
    assert set(used) == {"BatchGetItem"}


def test_the_tool_policy_names_one_action_on_one_table():
    p = genai.tool_policy("abc-updates", "111122223333")
    (st,) = p["Statement"]
    assert st["Action"] == ["dynamodb:BatchGetItem"] and st["Resource"].endswith(":table/abc-updates")


@pytest.mark.parametrize("start,end", [
    ("2026-11-12T16:00:00.000Z", "2026-11-12T15:00:00.000Z"),     # end before start
    ("2026-11-12T15:00:30.000Z", "2026-11-12T16:00:00.000Z"),     # not a whole minute
    ("yesterday", "2026-11-12T16:00:00.000Z"),                    # not a time
    ("2026-11-10T00:00:00.000Z", "2026-11-12T16:00:00.000Z"),     # more than a day
    (None, "2026-11-12T16:00:00.000Z"),                           # missing
])
def test_the_tool_refuses_bad_windows(start, end):
    with pytest.raises(ValueError):
        genai.count_updates(None, "t", start, end)


def test_a_refused_tool_call_reaches_the_model_as_an_error(table, as_of):
    tool, _, _ = table
    start, end = window(as_of)
    bad = StubClient(start, end, args={"start": "yesterday", "end": iso(end)})
    answer = genai.ask("q", bad, "stub", tool, start, end)
    assert answer["results"] == [] and "could not read" in answer["reply"]
    assert not genai.grounded(answer)


def test_the_loop_stops_after_the_step_limit(table, as_of):
    tool, _, _ = table
    start, end = window(as_of)
    call = NS(id="c", type="function",
              function=NS(name="count_updates", arguments=json.dumps({"start": iso(start), "end": iso(end)})))
    always_tool = NS(chat=NS(completions=NS(create=lambda **kw: NS(
        choices=[NS(message=NS(content="", tool_calls=[call]))], usage=None))))
    answer = genai.ask("q", always_tool, "stub", tool, start, end, max_steps=3)
    assert answer["steps"] == 3 and answer["reply"].startswith("Stopped after 3")


def test_a_reply_with_a_number_the_tool_did_not_return_is_flagged():
    assert not genai.grounded({"reply": "There were 300 updates.", "results": [{"updates": 287}]})
    assert genai.grounded({"reply": "There were 1,287 updates.", "results": [{"updates": 1287}]})


def test_the_lake_example_gives_the_same_number(table, local_root, as_of):
    _, expected, _ = table
    rows = query(local_root, lake_example.events_sql(*window(as_of)))
    assert int(rows[0]["updates_last_hour"]) == expected


GATEWAY_VARS = ("COURSE_GATEWAY_URL", "GATEWAY_URL", "KB_MCP_TOKEN", "COURSE_MCP_TOKEN")


def test_gateway_client_takes_url_and_token_from_the_environment(monkeypatch):
    for name in GATEWAY_VARS:
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(SystemExit):
        genai.gateway_client()
    monkeypatch.setenv("COURSE_GATEWAY_URL", "https://gateway.example/v1")
    monkeypatch.setenv("KB_MCP_TOKEN", "not-a-real-token")
    c = genai.gateway_client()                 # builds the client; makes no request
    assert str(c.base_url).startswith("https://gateway.example/v1") and c.api_key == "not-a-real-token"


def test_kb_mcp_token_wins_and_the_older_names_still_work(monkeypatch):
    for name in GATEWAY_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GATEWAY_URL", "https://older.example/v1")
    monkeypatch.setenv("COURSE_MCP_TOKEN", "older-token")
    assert genai.gateway_env() == ("https://older.example/v1", "older-token")
    monkeypatch.setenv("COURSE_GATEWAY_URL", "https://gateway.example/v1")
    monkeypatch.setenv("KB_MCP_TOKEN", "new-token")
    assert genai.gateway_env() == ("https://gateway.example/v1", "new-token")


def test_a_local_env_file_is_not_read(monkeypatch, tmp_path):
    # a .env in the folder must not change what the tests (or the recipe) see: only the environment counts
    for name in GATEWAY_VARS:
        monkeypatch.delenv(name, raising=False)
    (tmp_path / ".env").write_text("KB_MCP_TOKEN=from-a-file\nCOURSE_GATEWAY_URL=https://file.example/v1\n")
    monkeypatch.chdir(tmp_path)
    assert genai.gateway_env() == ("", "")
    with pytest.raises(SystemExit):
        genai.gateway_client()


def test_the_skeleton_folder_ignores_a_env_file():
    assert ".env" in (Path(__file__).resolve().parents[1] / ".gitignore").read_text().split()


def test_no_temperature_for_gpt_models():
    assert genai.sampling("qwen3.6-35b-a3b") == {"temperature": 0}
    for model in ("gpt-4o", "openai/gpt-4.1", "o3"):
        assert genai.sampling(model) == {}

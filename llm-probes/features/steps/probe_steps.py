"""Steps for main/probe.py and the capability groups, against the endpoint stand-in (features/support.py)."""

from __future__ import annotations

import importlib.util
import json
import os
import shlex
from pathlib import Path

import httpx
from behave import given, then, when
from support import KEY, URL, chat_reply, run_captured, table_rows

ROOT = Path(__file__).resolve().parent.parent.parent
_spec = importlib.util.spec_from_file_location("probe", ROOT / "main" / "probe.py")
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)

ONE_WORD_SYSTEM = "You answer with one word."


# ---- what a request is ----

def _has_key(request, body):
    return request.headers.get("authorization") == f"Bearer {KEY}"


def _is_one_word_chat(request, body):
    messages = body.get("messages") or [{}]
    return _has_key(request, body) and messages[0].get("content") == ONE_WORD_SYSTEM and not body.get("stream")


def _is_stream(request, body):
    return _has_key(request, body) and bool(body.get("stream"))


def _offers_tool(request, body):
    return _has_key(request, body) and bool(body.get("tools")) and body["messages"][-1]["role"] != "tool"


def _carries_tool_result(request, body):
    return _has_key(request, body) and bool(body.get("tools")) and body["messages"][-1]["role"] == "tool"


def _is_long_prompt(request, body):
    messages = body.get("messages") or [{}]
    return _has_key(request, body) and len(messages[0].get("content") or "") > 2000


def _error(status):
    return lambda request, body: httpx.Response(status, json={"error": {"message": "refused by the stand-in"}})


def _no_reply(request, body):
    raise httpx.ReadTimeout("the stand-in did not answer", request=request)


# ---- given ----

@given("LLM_BASE_URL and LLM_API_KEY are set to the stand-in endpoint and its key")
def env_set(context):
    os.environ["LLM_BASE_URL"], os.environ["LLM_API_KEY"] = URL, KEY


@given('LLM_API_KEY is "{key}"')
def other_key(context, key):
    os.environ["LLM_API_KEY"] = key


@given("{variable} is not set")
def not_set(context, variable):
    os.environ.pop(variable, None)


@given("the endpoint does nothing right beyond listing models and chatting")
def bad_endpoint(context):
    context.endpoint.good = False


@given("the endpoint answers GET /models with HTTP {status:d}")
def models_fail(context, status):
    context.endpoint.when(lambda r, b: r.method == "GET" and _has_key(r, b), _error(status))


@given("the endpoint answers a made-up key with HTTP {status:d}")
def wrong_key_status(context, status):
    def answer(request, body):
        if status == 200:
            return httpx.Response(200, json={"data": [{"id": "fake-chat"}]})
        return _error(status)(request, body)

    context.endpoint.when(lambda r, b: not _has_key(r, b), answer)


@given("the endpoint does not answer a made-up key in time")
def wrong_key_timeout(context):
    context.endpoint.when(lambda r, b: not _has_key(r, b), _no_reply)


@given("the endpoint answers a model name it does not know with HTTP {status:d}")
def unknown_model_status(context, status):
    answer = (lambda r, b: chat_reply("ready")) if status == 200 else _error(status)
    context.endpoint.when(lambda r, b: _has_key(r, b) and b.get("model") == "no-such-model", answer)


@given('the endpoint answers the one-word chat with "{text}", {out:d} tokens out, {reasoning:d} of them reasoning')
def one_word_reasoning(context, text, out, reasoning):
    context.endpoint.when(_is_one_word_chat, lambda r, b: chat_reply(text, tokens_out=out, reasoning=reasoning))


@given("the endpoint answers the one-word chat with HTTP {status:d}")
def one_word_status(context, status):
    context.endpoint.when(lambda r, b: _is_one_word_chat(r, b) and b.get("model") != "no-such-model", _error(status))


@given("the endpoint answers the one-word chat with an empty reply")
def one_word_empty(context):
    context.endpoint.when(lambda r, b: _is_one_word_chat(r, b) and b.get("model") != "no-such-model",
                          lambda r, b: chat_reply(""))


@given("the endpoint answers the one-word chat with a different word each time")
def one_word_changes(context):
    words = iter(["ready", "ready", "steady", "go", "now"])
    context.endpoint.when(lambda r, b: _is_one_word_chat(r, b) and b.get("model") != "no-such-model",
                          lambda r, b: chat_reply(next(words)))


@given("the endpoint streams {n:d} reasoning chunks and then the text")
def stream_reasoning(context, n):
    chunks = ['data: {"choices": [{"delta": {"reasoning_content": "hm"}}]}'] * n
    chunks += ['data: {"choices": [{"delta": {"content": "ready"}}]}',
               'data: {"choices": [], "usage": {"prompt_tokens": 65, "completion_tokens": 30}}', "data: [DONE]"]
    context.endpoint.when(_is_stream, lambda r, b: httpx.Response(
        200, text="\n\n".join(chunks) + "\n\n", headers={"content-type": "text/event-stream"}))


@given("the endpoint answers a streamed request with HTTP {status:d}")
def stream_status(context, status):
    context.endpoint.when(_is_stream, _error(status))


@given("the endpoint answers a streamed request with no reply")
def stream_no_reply(context):
    context.endpoint.when(_is_stream, _no_reply)


@given('the endpoint answers the request with max_tokens 5 with finish_reason "{finish}" and {out:d} tokens out')
def max_tokens_answer(context, finish, out):
    context.endpoint.when(lambda r, b: _has_key(r, b) and b.get("max_tokens") == 5,
                          lambda r, b: chat_reply("1\n2\n3", finish=finish, tokens_out=out))


@given('the endpoint answers the request with max_tokens 5 with an empty reply, finish_reason "{finish}" and {out:d} tokens out')
def max_tokens_empty(context, finish, out):
    context.endpoint.when(lambda r, b: _has_key(r, b) and b.get("max_tokens") == 5,
                          lambda r, b: chat_reply("", finish=finish, tokens_out=out))


@given('the endpoint answers the request that carries a tool result with "{text}"')
def tool_result_ignored(context, text):
    context.endpoint.when(_carries_tool_result, lambda r, b: chat_reply(text))


@given("the endpoint answers the request that offers a tool with {answer}")
def tool_offer_answer(context, answer):
    def reply(request, body):
        if answer == "plain text":
            return chat_reply("I think it has about a thousand rows.")
        arguments = "not json" if answer.endswith("not JSON") else json.dumps({"sql": "SELECT 1"})
        call = {"id": "call-1", "type": "function", "function": {"name": "run_sql", "arguments": arguments}}
        return httpx.Response(200, json={"choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": None, "tool_calls": [call]}}], "usage": {"prompt_tokens": 9, "completion_tokens": 9}})

    context.endpoint.when(_offers_tool, reply)


@given("the endpoint answers a request with a response format with the text {text}")
def response_format_answer(context, text):
    context.endpoint.when(lambda r, b: _has_key(r, b) and bool(b.get("response_format")), lambda r, b: chat_reply(text))


@given("the endpoint answers the long prompt with {cached} cached tokens")
def long_prompt_answer(context, cached):
    value = None if cached == "no" else int(cached)
    context.endpoint.when(_is_long_prompt, lambda r, b: chat_reply("Section 7.", cached=value))


@given("the endpoint answers the embeddings request with the vectors {vectors}")
def embeddings_answer(context, vectors):
    data = [{"index": i, "embedding": v} for i, v in enumerate(json.loads(vectors))]
    context.endpoint.when(lambda r, b: _has_key(r, b) and r.url.path.endswith("/embeddings"),
                          lambda r, b: httpx.Response(200, json={"data": data}))


@given('the endpoint\'s replies carry the header "{a}: {av}" and the header "{b}: {bv}"')
def reply_headers(context, a, av, b, bv):
    context.endpoint.headers = {a: av, b: bv}


# ---- when ----

def _run(context, argv):
    run_captured(context, probe.main, argv, transport=context.endpoint.transport())


@when("main/probe.py runs")
def probe_runs(context):
    _run(context, [])


@when('main/probe.py runs with "{args}" and a file in the temporary folder')
def probe_runs_json(context, args):
    context.json_file = context.tmp / "rows.json"
    _run(context, [*shlex.split(args), str(context.json_file)])


@when('main/probe.py runs with "{args}"')
def probe_runs_with(context, args):
    _run(context, shlex.split(args))


@when("main/probe.py runs from the temporary folder")
def probe_runs_in_tmp(context):
    before = os.getcwd()
    os.chdir(context.tmp)
    try:
        _run(context, [])
    finally:
        os.chdir(before)


# ---- then: the table ----

def _row(context, name, model=None):
    rows = [r for r in table_rows(context.printed) if r["probe"] == name and (model is None or r["model"] == model)]
    assert len(rows) == 1, (name, model, [r["probe"] for r in table_rows(context.printed)])
    return rows[0]


@then('the rows are "{names}"')
def rows_are(context, names):
    assert [r["probe"] for r in table_rows(context.printed)] == [n.strip() for n in names.split(",")], \
        [r["probe"] for r in table_rows(context.printed)]


@then('every row has works "yes", a time in seconds and a note')
def rows_all_yes(context):
    rows = table_rows(context.printed)
    assert rows and all(r["works"] == "yes" and float(r["seconds"]) >= 0 and r["note"] for r in rows), rows


@then('the row "{name}" says {works} with the note "{note}"')
def row_note(context, name, works, note):
    row = _row(context, name)
    assert (row["works"], row["note"]) == (works, note), row


@then('the row "{name}" says {works} with a note that starts with "{start}"')
def row_note_starts(context, name, works, start):
    row = _row(context, name)
    assert row["works"] == works and row["note"].startswith(start), row


@then('for "{model}" the row "{name}" says {works} with the note "{note}"')
def model_row_note(context, name, model, works, note):
    row = _row(context, name, model)
    assert (row["works"], row["note"]) == (works, note), row


@then('for "{model}" the row "{name}" says {works} with a note that starts with "{start}"')
def model_row_note_starts(context, name, model, works, start):
    row = _row(context, name, model)
    assert row["works"] == works and row["note"].startswith(start), row


@then('for "{model}" the row "{name}" says {works} with a note that ends with "{end}"')
def model_row_note_ends(context, name, model, works, end):
    row = _row(context, name, model)
    assert row["works"] == works and row["note"].endswith(end), row


@then('for "{model}" the row "{name}" says {works:w}')
def model_row(context, name, model, works):
    assert _row(context, name, model)["works"] == works, _row(context, name, model)


@then('the row "{name}" says {works:w}')
def row_says(context, name, works):
    assert _row(context, name)["works"] == works, _row(context, name)


@then('there is no row "{name}"')
def no_row(context, name):
    assert name not in [r["probe"] for r in table_rows(context.printed)]


@then('the first printed line names the host "{host}" and not the path or the key')
def first_line_host(context, host):
    first = context.printed.splitlines()[0]
    assert first.startswith(f"Endpoint {host}, ") and "/v1" not in first and KEY not in first, first


@then('the first printed line ends with "{text}"')
def first_line_ends(context, text):
    assert context.printed.splitlines()[0].endswith(text), context.printed.splitlines()[0]


# ---- then: exit code, printed output, files ----

@then("the exit code is {code:d}")
def exit_code(context, code):
    assert context.exit_code == code, (context.exit_code, context.printed_err[-400:])


@then('the error output holds "{text}"')
def err_holds(context, text):
    assert text in context.printed_err, context.printed_err


@then('the printed output holds "{text}"')
def out_holds(context, text):
    assert text in context.printed, context.printed[-600:]


@then('the printed output does not hold "{text}"')
def out_lacks(context, text):
    assert text not in context.printed


@then("the key is in no printed line and not in the file")
def key_nowhere(context):
    assert KEY not in context.printed + context.printed_err + context.json_file.read_text()
    # the scenario means something only if the endpoint did echo the key back
    assert any(KEY in r.read().decode() for r in _responses_with_key(context)), "the stand-in never echoed the key"


def _responses_with_key(context):
    transport = context.endpoint.transport()
    return [transport.handler(httpx.Request("POST", URL + "/chat/completions",
                                            headers={"authorization": f"Bearer {KEY}"},
                                            json={"model": "no-such-model", "messages": []}))]


@then('the file holds "{text}"')
def file_holds(context, text):
    assert text in context.json_file.read_text()


@then('the file holds {n:d} rows with "{fields}"')
def file_rows(context, n, fields):
    rows = json.loads(context.json_file.read_text())
    assert len(rows) == n and all(list(r) == [f.strip() for f in fields.split(",")] for r in rows), rows


@then("the rows in the file are the rows that were printed")
def file_rows_match(context):
    in_file = [(r["probe"], r["model"], r["works"]) for r in json.loads(context.json_file.read_text())]
    assert in_file == [(r["probe"], r["model"], r["works"]) for r in table_rows(context.printed)]


@then("the temporary folder is empty")
def tmp_empty(context):
    assert list(context.tmp.iterdir()) == [] and table_rows(context.printed)


# ---- then: what the endpoint received ----

@then("the endpoint received {n:d} request")
@then("the endpoint received {n:d} requests")
def received(context, n):
    assert len(context.endpoint.requests) == n, len(context.endpoint.requests)


@then('every request that carries the key carries it as "Authorization: Bearer" and nowhere else')
def key_only_in_header(context):
    with_key = [r for r in context.endpoint.requests if r.headers.get("authorization") == f"Bearer {KEY}"]
    assert len(with_key) >= 10
    for r in context.endpoint.requests:
        others = " ".join(f"{k}: {v}" for k, v in r.headers.items() if k != "authorization")
        assert KEY not in str(r.url) + r.content.decode() + others, r.url


@then('every request went to an address that starts with "{start}"')
def addresses(context, start):
    assert all(str(r.url).startswith(start) for r in context.endpoint.requests)


@then("the request with the made-up key is the only one without the key")
def one_without_key(context):
    without = [r for r in context.endpoint.requests if r.headers.get("authorization") != f"Bearer {KEY}"]
    assert [r.headers["authorization"] for r in without] == ["Bearer not-a-real-key"]


@then('no chat request named the model "{model}"')
def no_chat_for(context, model):
    chats = [r for r in context.endpoint.requests if r.url.path.endswith("/chat/completions")]
    assert chats and all(json.loads(r.content)["model"] != model for r in chats)


@then("every chat request has temperature 0")
def all_temperature_0(context):
    chats = [b for b in context.endpoint.bodies() if "messages" in b]
    assert len(chats) >= 10 and {b["temperature"] for b in chats} == {0}


@then("every chat request has max_tokens of {n:d} or less")
def all_max_tokens(context, n):
    chats = [b for b in context.endpoint.bodies() if "messages" in b]
    assert max(b["max_tokens"] for b in chats) == n, sorted({b["max_tokens"] for b in chats})


@then("the one-word chat asks for at most {n:d} tokens")
def one_word_max(context, n):
    one = [b for b in context.endpoint.bodies() if (b.get("messages") or [{}])[0].get("content") == ONE_WORD_SYSTEM]
    assert one and {b["max_tokens"] for b in one} == {n}


@then('the only requests are "{kinds}"')
def only_requests(context, kinds):
    assert {f"{r.method} {r.url.path}" for r in context.endpoint.requests} == {k.strip() for k in kinds.split(",")}


@then("the streamed request asked for stream true and usage in the stream")
def stream_request(context):
    streams = [b for b in context.endpoint.bodies() if b.get("stream")]
    assert len(streams) == 1 and streams[0]["stream"] is True
    assert streams[0]["stream_options"] == {"include_usage": True}


@then("the request that counts to 50 has max_tokens {n:d}")
def count_request(context, n):
    counts = [b for b in context.endpoint.bodies() if "Count from 1 to 50" in json.dumps(b.get("messages", ""))]
    assert len(counts) == 1 and counts[0]["max_tokens"] == n


@then('the second tool request carries the tool result "{number}" for the call "{call_id}"')
def tool_result_sent(context, number, call_id):
    second = [b for b in context.endpoint.bodies() if b.get("tools") and b["messages"][-1]["role"] == "tool"]
    assert len(second) == 1
    last = second[0]["messages"][-1]
    assert last["tool_call_id"] == call_id and json.loads(last["content"]) == {"rows": [[int(number)]]}, last
    assert second[0]["tools"][0]["function"]["name"] == "run_sql"


@then("{n:d} request offered a tool")
def tool_requests(context, n):
    assert sum(bool(b.get("tools")) for b in context.endpoint.bodies()) == n


@then("every request with a response format sends the strict schema with city and population")
def schema_requests(context):
    formats = [b["response_format"] for b in context.endpoint.bodies() if b.get("response_format")]
    assert len(formats) == 2
    for f in formats:
        assert f["type"] == "json_schema" and f["json_schema"]["strict"] is True
        assert set(f["json_schema"]["schema"]["properties"]) == {"city", "population"}


@then('the last request with a response format asks for "{text}"')
def schema_prose(context, text):
    last = [b for b in context.endpoint.bodies() if b.get("response_format")][-1]
    assert text in last["messages"][-1]["content"]


@then("{n:d} requests carried the same system prompt of more than {chars:d} characters, with the question last")
def long_prompts(context, n, chars):
    longs = [b for b in context.endpoint.bodies() if len((b.get("messages") or [{}])[0].get("content") or "") > chars]
    assert len(longs) == n and longs[0] == longs[1]
    assert [m["role"] for m in longs[0]["messages"]] == ["system", "user"]
    assert len(longs[0]["messages"][1]["content"]) < 100


@then("the embeddings request sent {n:d} sentences to POST /embeddings")
def embeddings_request(context, n):
    sent = [r for r in context.endpoint.requests if r.url.path.endswith("/embeddings")]
    assert len(sent) == 1 and len(json.loads(sent[0].content)["input"]) == n


# ---- more scenarios ----

@given("the endpoint answers GET /models with an empty list")
def models_empty(context):
    context.endpoint.when(lambda r, b: r.method == "GET" and _has_key(r, b),
                          lambda r, b: httpx.Response(200, json={"object": "list", "data": []}))


@given("OPENAI_API_KEY and OPENAI_BASE_URL are set to the stand-in endpoint and its key")
def openai_env(context):
    os.environ["OPENAI_BASE_URL"], os.environ["OPENAI_API_KEY"] = URL, KEY


def _long_error(request, body):
    text = "e" * 100 + KEY + "f" * (300 - 100 - len(KEY))
    assert len(text) == 300 and text.index(KEY) == 100   # the note keeps 110 characters: the cut is inside the key
    return httpx.Response(404, json={"error": {"message": text}})


@given("the endpoint answers a model name it does not know with an error of 300 characters that has the key at character 100")
def long_error_unknown_model(context):
    context.endpoint.when(lambda r, b: _has_key(r, b) and b.get("model") == "no-such-model", _long_error)


@given("the endpoint answers a streamed request with an error of 300 characters that has the key at character 100")
def long_error_stream(context):
    context.endpoint.when(_is_stream, _long_error)


@given("the endpoint answers a model name it does not know with an error that holds the last 10 characters of the key")
def partial_echo(context):
    context.endpoint.when(lambda r, b: _has_key(r, b) and b.get("model") == "no-such-model",
                          lambda r, b: httpx.Response(404, json={"error": {"message": f"key ending in {KEY[-10:]} may not use it"}}))


@given("the endpoint answers the one-word chat with status 200 and no choices")
def one_word_no_choices(context):
    context.endpoint.when(lambda r, b: _is_one_word_chat(r, b) and b.get("model") != "no-such-model",
                          lambda r, b: httpx.Response(200, json={"error": {"message": "upstream failed"}}))


@given("the endpoint streams a chunk that is not JSON")
def stream_bad_chunk(context):
    context.endpoint.when(_is_stream, lambda r, b: httpx.Response(
        200, text="data: {not json\n\ndata: [DONE]\n\n", headers={"content-type": "text/event-stream"}))


@then('the row "{name}" has a note of at most {n:d} characters')
def note_length(context, name, n):
    row = _row(context, name)
    assert 100 < len(row["note"]) <= n, row


def _pieces(key: str, n: int = 8):
    return {key[i:i + n] for i in range(len(key) - n + 1)}


@then("no piece of the key of 8 characters or more is in a printed line or in the file")
def no_piece(context):
    text = context.printed + context.printed_err + context.json_file.read_text()
    found = [piece for piece in _pieces(KEY) if piece in text]
    assert not found, found


@then("every request was sent with a time limit of {seconds:g} seconds for the reply")
def request_timeouts(context, seconds):
    limits = {r.extensions["timeout"]["read"] for r in context.endpoint.requests}
    assert len(context.endpoint.requests) >= 5 and limits == {seconds}, limits


@then('the last printed line is "{text}"')
def last_line_is(context, text):
    assert context.printed.splitlines()[-1] == text, context.printed.splitlines()[-1]


# ---- main/compare.py ----

_cspec = importlib.util.spec_from_file_location("compare", ROOT / "main" / "compare.py")
compare = importlib.util.module_from_spec(_cspec)
_cspec.loader.exec_module(compare)


def _saved_run(context, name, argv):
    path = context.tmp / name
    run_captured(context, probe.main, [*argv, "--json", str(path)], transport=context.endpoint.transport())
    assert path.exists(), context.printed_err
    context.printed = ""


@given('a run of every group saved as "{name}"')
def saved_run(context, name):
    _saved_run(context, name, [])


@given('a run of the groups "{groups}" saved as "{name}"')
def saved_run_groups(context, groups, name):
    _saved_run(context, name, ["--groups", groups])


@when('main/compare.py compares "{first}" and "{second}"')
def compare_runs(context, first, second):
    context.requests_before = len(context.endpoint.requests)
    run_captured(context, compare.main, [str(context.tmp / first), str(context.tmp / second)])


def _compared(context) -> list[list[str]]:
    lines = [line for line in context.printed.splitlines() if line.startswith("| ") and "|---" not in line][1:]
    return [[c.strip() for c in line.strip("|").split("|")] for line in lines]


@then('the comparison lists "{names}"')
def comparison_lists(context, names):
    assert [row[0] for row in _compared(context)] == [n.strip() for n in names.split(",")], _compared(context)


@then('the comparison says "{name}" went from "{before}" to "{after}"')
def comparison_row(context, name, before, after):
    row = next(r for r in _compared(context) if r[0] == name)
    assert row[2:] == [before, after], row


@then("the comparison sent no request to the endpoint")
def comparison_offline(context):
    assert len(context.endpoint.requests) == context.requests_before


# ---- two chat models, usage shapes ----

@given('the endpoint lists the chat models "{a}" and "{b}"')
def two_chat_models(context, a, b):
    context.endpoint.models = [a, b, "fake-embed"]


@given('the endpoint answers the one-word chat of "{model}" with HTTP {status:d}')
def one_word_status_model(context, model, status):
    context.endpoint.when(lambda r, b: _is_one_word_chat(r, b) and b.get("model") == model, _error(status))


@given('the endpoint answers "{model}" with plain text where a tool is offered')
def tool_offer_plain(context, model):
    context.endpoint.when(lambda r, b: _offers_tool(r, b) and b.get("model") == model,
                          lambda r, b: chat_reply("I think it has about a thousand rows."))


@given("the endpoint answers the one-word chat with a usage of {total:d} tokens in total and {out:d} out, and no input count")
def one_word_total_only(context, total, out):
    def reply(request, body):
        return httpx.Response(200, json={"choices": [{"index": 0, "finish_reason": "stop", "message": {
            "role": "assistant", "content": "ready"}}], "usage": {"completion_tokens": out, "total_tokens": total}})
    context.endpoint.when(lambda r, b: _is_one_word_chat(r, b) and b.get("model") != "no-such-model", reply)


@then('the row "{name}" of "{model}" says "{works}"')
def row_of_model(context, name, model, works):
    found = [r for r in table_rows(context.printed) if r["probe"] == name and r["model"] == model]
    assert len(found) == 1 and found[0]["works"] == works, found


@then('the comparison lists only rows of "{model}"')
def comparison_only(context, model):
    assert _compared(context) and {row[1] for row in _compared(context)} == {model}, _compared(context)


@then('for "{model}" the comparison row "{name}" reads "{before}" then "{after}"')
def comparison_row_model(context, model, name, before, after):
    rows = [r for r in _compared(context) if r[0] == name and r[1] == model]
    assert len(rows) == 1 and rows[0][2:] == [before, after], _compared(context)


# ---- closure polish: a streamed error body cut inside the key ----

@given("the endpoint answers a streamed request with an error body of plain text that has the key at character {at:d}")
def stream_error_body(context, at):
    text = "e" * at + KEY + "f" * 100
    assert text.index(KEY) == at and at < 110 < at + 8      # the cut leaves fewer than 8 characters of the key
    context.endpoint.when(_is_stream, lambda r, b: httpx.Response(500, text=text))


@then("the first {n:d} characters of the key are in no printed line and not in the file")
def no_prefix(context, n):
    text = context.printed + context.printed_err + context.json_file.read_text()
    assert "streaming" in text and KEY[:n] not in text, [line for line in text.splitlines() if KEY[:n] in line]

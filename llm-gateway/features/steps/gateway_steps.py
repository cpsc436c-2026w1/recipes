"""Steps for the client (main/call_gateway.py) and for the gateway's own code (ext-deploy), over HTTP on 127.0.0.1."""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import secrets as py_secrets
import shlex
import socket
import sys
import tempfile
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import boto3
import httpx
from behave import given, then, when
from moto.sns.models import sns_backends

ROOT = Path(__file__).resolve().parent.parent.parent
_spec = importlib.util.spec_from_file_location("call_gateway", ROOT / "main" / "call_gateway.py")
client_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = client_mod
_spec.loader.exec_module(client_mod)

HI = [{"role": "user", "content": "hi"}]


# ---- helpers ----

def _admin(context, *argv) -> str:
    return context.harness.run_admin(*argv)


def _issue(context, *flags):
    out = _admin(context, "issue", "--owner", context.owner, "--kind", "person", *flags)
    context.issue_output = out
    context.key = out.strip().splitlines()[-1]
    context.key_id = context.harness.auth.KEY_RE.match(context.key).group(1)


def _set_env(context, name, value):
    old = os.environ.get(name)
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value
    context.add_cleanup(lambda: os.environ.__setitem__(name, old) if old is not None else os.environ.pop(name, None))


def _post(context, body=None, *, key=None, raw=None, stream=False):
    headers = {"Authorization": f"Bearer {key if key is not None else context.key}"} if key != "" else {}
    with httpx.Client(base_url=context.gateway_url, timeout=30) as c:
        if raw is not None:
            r = c.post("/v1/chat/completions", content=raw, headers={**headers, "Content-Type": "application/json"})
        else:
            r = c.post("/v1/chat/completions", json=body, headers=headers)
    context.response = r
    context.answers = [*getattr(context, "answers", []), r.status_code]
    if r.status_code == 200 and context.key_id:
        context.harness.wait_for_usage(context.key_id)          # the settle runs in the gateway's own thread
    return r


def _chat(**kw) -> dict:
    return context_free_chat(**kw)


def context_free_chat(**kw) -> dict:
    body = {"model": "example-large", "messages": HI}
    body.update(kw)
    return body


def _spent(context) -> int:
    deadline = time.time() + 5
    last = None
    while time.time() < deadline:                                 # two reads that agree: no settle is under way
        now = int(context.harness.key_item(context.key_id).get("spent_micro", 0))
        if now == last:
            return now
        last = now
        time.sleep(0.15)
    return last


def _received(context) -> list[dict]:
    return context.harness.fake_upstream.received


# ---- given: keys ----

@given("a key with every example model")
def key_default(context):
    _issue(context)


@given("a key with a budget of {amount:g} and a max_tokens cap of {cap:d}")
def key_budget_cap(context, amount, cap):
    _issue(context, "--budget", f"{amount:f}")
    context.harness.set_attr(context.key_id, "max_tokens_cap", cap)


@given("a key with a budget of {amount}")
def key_budget(context, amount):
    _issue(context, "--budget", amount)


@given("a key for the model example-small only")
def key_small_only(context):
    _issue(context, "--models", "example-small")


@given("a key with a rate limit of {n:d} request a minute")
@given("a key with a rate limit of {n:d} requests a minute")
def key_rate(context, n):
    _issue(context)
    _admin(context, "rate", "set", context.key_id, str(n))


@given("a key that was revoked")
def key_revoked(context):
    _issue(context)
    _admin(context, "revoke", context.key_id)


@given("a key that expired yesterday")
def key_expired(context):
    _issue(context)
    context.harness.set_attr(context.key_id, "expires_at", int(time.time()) - 86_400)


@given("a key the gateway does not know")
def key_unknown(context):
    context.key = "gw-ABCDEFGH-" + py_secrets.token_urlsafe(32)
    context.key_id = None


@given('a key that another system made, imported by its SHA-256 for the owner "{owner}"')
def key_imported(context, owner):
    context.key = py_secrets.token_urlsafe(32)                    # 43 url-safe characters, never shown to the gateway
    context.key_hash = hashlib.sha256(context.key.encode()).hexdigest()
    context.imported_owner = owner
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "hashes.json"
        path.write_text(json.dumps({context.key_hash: owner}))
        context.import_output = _admin(context, "import", str(path), "--team-sep", "/", "--budget", "1")
    context.key_id = context.harness.admin.imported_key_id(context.key_hash)


# ---- given: the client's environment ----

@given("COURSE_GATEWAY_URL and KB_MCP_TOKEN are set to the gateway and the key")
def client_env(context):
    _set_env(context, "COURSE_GATEWAY_URL", context.gateway_url + "/v1")
    _set_env(context, "KB_MCP_TOKEN", context.key)


@given("GATEWAY_URL and COURSE_MCP_TOKEN are set to the gateway and the key")
def client_env_older_names(context):
    _set_env(context, "GATEWAY_URL", context.gateway_url + "/v1")
    _set_env(context, "COURSE_MCP_TOKEN", context.key)


@given('the variable {variable} is set to "{value}"')
def env_set_to(context, variable, value):
    _set_env(context, variable, value)


@given("{variable} is not set")
def not_set(context, variable):
    _set_env(context, variable, None)


@given("COURSE_GATEWAY_URL is an address where nothing listens")
def gateway_gone(context):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    _set_env(context, "COURSE_GATEWAY_URL", f"http://127.0.0.1:{port}/v1")


@given('the gateway\'s replies carry no "{name}" header')
def gateway_without_header(context, name):
    """As the course's own gateway may answer: the same reply, without that header. The client gets its replies
    through an httpx hook that takes the header out."""
    context.drop_header = name


@then('the last printed line is "{text}"')
def last_line_is(context, text):
    assert context.printed.splitlines()[-1].strip() == text, context.printed.splitlines()[-1]


@then('the last printed line starts with "{text}"')
def last_line_starts(context, text):
    assert context.printed.splitlines()[-1].strip().startswith(text), context.printed.splitlines()[-1]


@when("the key sends a chat whose reply carries no usage")
def key_chat_no_usage(context):
    _post(context, context_free_chat(max_tokens=100, x_fake={"no_usage": True}))


@then('the reply has no "{name}" header')
def reply_no_header(context, name):
    assert name not in context.response.headers, dict(context.response.headers)


@then("the error output is empty")
def err_empty(context):
    assert context.printed_err == "", context.printed_err


@then("the client's time limit for one request is {seconds:g} seconds")
def client_time_limit(context, seconds):
    t = context.client.http.timeout
    assert (t.connect, t.read, t.write, t.pool) == (seconds,) * 4, t


@given("every request the client sends is recorded")
def record_client(context):
    context.client_requests = []


@given("every connection to an address other than 127.0.0.1 fails the scenario")
def only_localhost(context):
    built = socket.socket.connect

    def connect(self, address):
        host = address[0] if isinstance(address, tuple) else address
        assert host == "127.0.0.1", f"the run opened a connection to {address}"
        return built(self, address)

    socket.socket.connect = connect
    context.add_cleanup(setattr, socket.socket, "connect", built)


@given("the model service answers every request with 429 and Retry-After {seconds:d}")
def upstream_429(context, seconds):
    _busy_upstream(context, str(seconds))


@given("the model service answers every request with 429 and no Retry-After")
def upstream_429_plain(context):
    _busy_upstream(context, None)


def _busy_upstream(context, retry_after):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            out = b'{"error": {"message": "busy"}}'
            self.send_response(429)
            if retry_after:
                self.send_header("Retry-After", retry_after)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    context.add_cleanup(server.shutdown)
    _set_env(context, "UPSTREAM_BASE_URL", f"http://127.0.0.1:{server.server_port}/v1")


@given('the model service answers every chat with no text and finish_reason "length"')
def upstream_cap_used_up(context):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
            out = json.dumps({
                "id": "chatcmpl-cap", "object": "chat.completion", "model": body["model"],
                "choices": [{"index": 0, "message": {"role": "assistant", "content": ""}, "finish_reason": "length"}],
                "usage": {"prompt_tokens": 18, "completion_tokens": body["max_tokens"],
                          "total_tokens": 18 + body["max_tokens"]}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    context.add_cleanup(server.shutdown)
    _set_env(context, "UPSTREAM_BASE_URL", f"http://127.0.0.1:{server.server_port}/v1")


@given("the upstream's address is one where nothing listens")
def upstream_gone(context):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    _set_env(context, "UPSTREAM_BASE_URL", f"http://127.0.0.1:{port}/v1")


@given("the gateway waits {seconds:g} seconds for the upstream")
def read_timeout(context, seconds):
    _set_env(context, "UPSTREAM_READ_TIMEOUT_S", str(seconds))


@given("the text log is off")
def text_log_off(context):
    _set_env(context, "TABLE_TEXTS", None)


@given("the text log is on")
def text_log_on(context):
    assert os.environ.get("TABLE_TEXTS") == "llm-gateway-texts"


# ---- when: the client ----

def _run_client(context, argv):
    """main() of the client, with two things added to its Gateway: a wait that is recorded and not slept, and a
    record of every request it sends. The requests, the retry rule and the printing are the client's own."""
    built = client_mod.Gateway
    requests = getattr(context, "client_requests", None)

    class Recorded(built):
        def __init__(self, base_url, token):
            limit = getattr(context, "client_timeout", None)
            super().__init__(base_url, token, sleep=context.slept.append,
                             **({"timeout_s": limit} if limit else {}))
            context.client = self
            drop = getattr(context, "drop_header", None)
            if drop:
                self.http.event_hooks["response"].append(lambda r: r.headers.pop(drop, None))
            if requests is not None:
                self.http.event_hooks["request"].append(requests.append)

    client_mod.Gateway = Recorded
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                context.exit_code = client_mod.main(argv)
            except SystemExit as e:
                context.exit_code = e.code if isinstance(e.code, int) else 1
                if not isinstance(e.code, int) and e.code is not None:
                    err.write(str(e.code) + "\n")
    finally:
        client_mod.Gateway = built
    context.printed += out.getvalue()
    context.printed_err += err.getvalue()


@when("main/call_gateway.py runs")
def client_runs(context):
    _run_client(context, [])


@when('main/call_gateway.py runs with "{args}"')
def client_runs_with(context, args):
    _run_client(context, shlex.split(args))


@when('main/call_gateway.py runs from an empty folder with "{args}"')
def client_runs_in_empty_folder(context, args):
    before = os.getcwd()
    context.folder = tempfile.mkdtemp(prefix="llm-gateway-scenario-")
    os.chdir(context.folder)
    try:
        _run_client(context, shlex.split(args))
    finally:
        os.chdir(before)


# ---- when: requests to the gateway ----

@when("a chat is sent with no Authorization header")
def chat_no_key(context):
    _post(context, context_free_chat(), key="")


@when("a chat is sent with a key of another shape")
def chat_odd_key(context):
    _post(context, context_free_chat(), key="sk-not-a-gateway-key")


@when("a chat is sent with a well-formed key that was never issued")
def chat_unknown_key(context):
    _post(context, context_free_chat(), key="gw-ABCDEFGH-" + py_secrets.token_urlsafe(32))


@when("the key sends a chat")
def key_chat(context):
    _post(context, context_free_chat())


@when("the key sends a chat without max_tokens")
def key_chat_no_max(context):
    _post(context, context_free_chat())


@when("the key sends a chat with max_tokens {n:d}")
def key_chat_max(context, n):
    _post(context, context_free_chat(max_tokens=n))


@when("the key sends a chat with max_tokens {n:d} and the upstream writes {out:d} tokens")
def key_chat_out(context, n, out):
    _post(context, context_free_chat(max_tokens=n, x_fake={"completion_tokens": out}))


@when("the key sends a chat with max_tokens {n:d} and the upstream answers {status:d}")
def key_chat_status(context, n, status):
    _post(context, context_free_chat(max_tokens=n, x_fake={"status": status}))
    time.sleep(0.2)


@when("the key sends a chat with max_tokens {n:d} and the upstream takes {seconds:g} second")
def key_chat_slow(context, n, seconds):
    _post(context, context_free_chat(max_tokens=n, x_fake={"delay": seconds}))
    time.sleep(0.2)


@when('the key sends a chat and the upstream answers {status:d} with the message "{message}"')
def key_chat_status_message(context, status, message):
    _post(context, context_free_chat(x_fake={"status": status, "message": message}))
    time.sleep(0.2)


@when("the key sends a chat and the upstream answers 429 with Retry-After {seconds:d}")
def key_chat_429(context, seconds):
    _post(context, context_free_chat(x_fake={"status": 429, "retry_after": seconds}))
    time.sleep(0.2)


@when('the key sends a chat that says "{text}"')
def key_chat_text(context, text):
    context.said = text
    _post(context, context_free_chat(messages=[{"role": "user", "content": text}]))


@when("the key sends a streamed chat")
def key_chat_stream(context):
    _post(context, context_free_chat(stream=True))


@when("the key sends {n:d} chats that each cost 502 micro-units")
def key_chats_502(context, n):
    body = context_free_chat(max_tokens=10, messages=[{"role": "user", "content": "x" * 4984}],
                             x_fake={"completion_tokens": 10})
    context.answers = []
    for _ in range(n):
        _post(context, body)
        _spent(context)


@when("the key sends {n:d} chats that each cost 202 micro-units")
def key_chats_202(context, n):
    body = context_free_chat(max_tokens=1000, x_fake={"completion_tokens": 1000})
    context.answers = []
    for _ in range(n):
        _post(context, body)
        _spent(context)


@when("the key sends a body that is not JSON")
def key_bad_json(context):
    _post(context, raw=b"{not json")


@when("the key sends a JSON list as the body")
def key_json_list(context):
    _post(context, raw=b"[1, 2]")


@when("the key sends a chat with n set to 2")
def key_n(context):
    _post(context, context_free_chat(n=2))


@when("the key sends a chat with best_of")
def key_best_of(context):
    _post(context, context_free_chat(best_of=2))


@when("the key sends a chat of 300,000 bytes")
def key_big(context):
    _post(context, context_free_chat(messages=[{"role": "user", "content": "x" * 300_000}]))


@when("the key asks for its models")
def key_models(context):
    with httpx.Client(base_url=context.gateway_url, timeout=30) as c:
        context.response = c.get("/v1/models", headers={"Authorization": f"Bearer {context.key}"})


# ---- when: gw-admin ----

@when("gw-admin revokes the key")
def admin_revoke(context):
    _admin(context, "revoke", context.key_id)


@when("gw-admin spend runs for the key")
def admin_spend(context):
    context.printed = _admin(context, "spend", "--key", context.key_id)


@when("gw-admin sets the key's budget to {amount}")
def admin_budget(context, amount):
    _admin(context, "budget", "set", context.key_id, amount)


@when('gw-admin runs "{args}"')
def admin_runs(context, args):
    context.printed = _admin(context, *shlex.split(args))


# ---- then: the client ----

@then("the exit code is {code:d}")
def exit_code(context, code):
    assert context.exit_code == code, (context.exit_code, context.printed, context.printed_err)


@then('the printed output holds "{text}"')
def printed_holds(context, text):
    assert text in context.printed, context.printed


@then('the printed output does not hold "{text}"')
def printed_lacks(context, text):
    assert text not in context.printed, context.printed


@then('the error output holds "{text}"')
def err_holds(context, text):
    assert text in context.printed_err, context.printed_err


@then('the printed lines are, in order, "{first}", the three models, "{second}", a reply and its tokens')
def printed_order(context, first, second):
    lines = [line.strip() for line in context.printed.splitlines()]
    assert lines[0] == first and lines[1] == "example-embed, example-large, example-small" and lines[2] == second
    assert lines[3] == "reply: 'bonjour'" and lines[4].startswith("tokens: "), lines


@then('the printed tokens are "{tokens}" with a request id')
def printed_tokens(context, tokens):
    line = next(x.strip() for x in context.printed.splitlines() if x.strip().startswith("tokens: "))
    assert re.fullmatch(rf"tokens: {tokens}; request id [0-9a-f-]{{36}}", line), line


@then("the client sent {n:d} requests, both to the gateway's address")
@then("the client sent {n:d} requests")
def client_sent(context, n):
    assert len(context.client_requests) == n, [str(r.url) for r in context.client_requests]
    assert all(str(r.url).startswith(context.gateway_url + "/v1/") for r in context.client_requests)


@then('each carried the token as "Authorization: Bearer" and nowhere else')
def token_in_header_only(context):
    for r in context.client_requests:
        assert r.headers["authorization"] == f"Bearer {context.key}"
        others = " ".join(f"{k}: {v}" for k, v in r.headers.items() if k != "authorization")
        assert context.key not in str(r.url) + r.content.decode() + others


@then("the token is in no printed line")
def token_not_printed(context):
    assert context.printed and context.key not in context.printed + context.printed_err


@then("the client did not wait")
def did_not_wait(context):
    assert context.slept == []


@then("the client waited once, for {seconds:d} seconds")
def waited_once(context, seconds):
    assert context.slept == [seconds], context.slept


@then("the folder is still empty")
def folder_empty(context):
    assert os.listdir(context.folder) == []


# ---- then: the gateway's answer ----

@then('the gateway answers {status:d} with the code "{code}"')
def answers_with_code(context, status, code):
    r = context.response
    assert r.status_code == status and r.json()["error"]["code"] == code, (r.status_code, r.text[:300])
    assert set(r.json()["error"]) == {"message", "type", "code"}        # the OpenAI error shape


@then("the gateway answers {status:d}")
def answers(context, status):
    assert context.response.status_code == status, (context.response.status_code, context.response.text[:300])


@then('the answers were "{codes}"')
def answers_were(context, codes):
    assert context.answers == [int(c) for c in codes.split(",")], context.answers


@then("the reply has no Retry-After header")
def no_retry_after(context):
    assert "retry-after" not in context.response.headers


@then('the reply has the header "{name}" with "{value}"')
def reply_header(context, name, value):
    assert context.response.headers.get(name) == value, dict(context.response.headers)


@then('the reply\'s error message is "{text}"')
def reply_error_message(context, text):
    assert context.response.json()["error"]["message"] == text, context.response.text[:300]


@then('the reply does not hold "{text}"')
def reply_lacks(context, text):
    assert text not in context.response.text


@then('the models listed are "{models}"')
def models_listed(context, models):
    assert [m["id"] for m in context.response.json()["data"]] == [m.strip() for m in models.split(",")]


# ---- then: the key, the usage, the upstream ----

@then("the key's spend is {micro:d} micro-units")
def spend_is(context, micro):
    assert _spent(context) == micro, _spent(context)


@then('the usage table has one row for the key, today (UTC) and "{model}": {n:d} request, {t_in:d} tokens in, {t_out:d} out, {micro:d} micro-units')
def usage_row(context, model, n, t_in, t_out, micro):
    [row] = context.harness.usage_rows(context.key_id)
    assert row["sk"] == f"{datetime.now(UTC):%Y-%m-%d}#{model}", row["sk"]
    assert (int(row["requests"]), int(row["tokens_in"]), int(row["tokens_out"]), int(row["cost_micro"])) == \
        (n, t_in, t_out, micro), row


@then("the upstream received {n:d} requests")
def upstream_received(context, n):
    assert len(_received(context)) == n, len(_received(context))


@then("the upstream received a chat with max_tokens {n:d} and temperature 0")
def upstream_chat_temp(context, n):
    [got] = _received(context)
    assert (got["body"]["max_tokens"], got["body"]["temperature"]) == (n, 0), got["body"]


@then("the upstream received a chat with max_tokens {n:d}")
def upstream_chat(context, n):
    [got] = _received(context)
    assert got["body"]["max_tokens"] == n, got["body"]


@then("the upstream received the provider key as its bearer key")
def upstream_key(context):
    [got] = _received(context)
    assert got["authorization"] == f"Bearer {context.harness.FAKE_UPSTREAM_KEY}"


@then("the caller's key is in nothing the upstream received")
def caller_key_not_upstream(context):
    assert context.key not in json.dumps(_received(context))


@then("the provider key is in nothing the caller received")
def provider_key_not_back(context):
    r = context.response
    assert context.harness.FAKE_UPSTREAM_KEY not in r.text + json.dumps(dict(r.headers))


@then("the bytes the caller received are the bytes the upstream sent")
def stream_unchanged(context):
    assert context.response.content == context.harness.fake_upstream.render_stream(3, (18, 3))


@then("the upstream was asked for usage in the stream")
def upstream_usage_asked(context):
    [got] = _received(context)
    assert got["body"]["stream"] is True and got["body"]["stream_options"] == {"include_usage": True}


# ---- then: keys in the table ----

@then("gw-admin issue printed the key once")
def issue_printed_once(context):
    assert context.issue_output.count(context.key) == 1


@then("the keys table holds the SHA-256 of the key and not the key")
def table_holds_hash(context):
    item = context.harness.key_item(context.key_id)
    assert item["key_hash"] == hashlib.sha256(context.key.encode()).hexdigest()
    assert context.key not in json.dumps(item, default=str)


@then("gw-admin list does not print the key")
def list_hides_key(context):
    listed = _admin(context, "list")
    assert context.key_id in listed and context.key not in listed


@then("gw-admin import printed counts and neither the owner nor the hash")
def import_output(context):
    out = context.import_output
    assert "imported 1 key" in out and "ana" not in out and context.key_hash not in out, out


@then('the imported key\'s id starts with "{prefix}"')
def imported_id(context, prefix):
    assert context.key_id.startswith(prefix)


# ---- then: notices ----

@then('the notices for the key\'s owner are "{subjects}"')
def notices_are(context, subjects):
    topic = sns_backends["111122223333"]["ca-central-1"].topics[context.topic_arn]
    sent = [n for n in topic.sent_notifications if n[3]["owner"]["Value"] == context.owner]
    prefix = f"llm gateway: {context.owner} "
    assert [n[2].removeprefix(prefix) for n in sent] == [s.strip() for s in subjects.split(",")], [n[2] for n in sent]
    assert all(context.harness.FAKE_UPSTREAM_KEY not in n[1] and context.key not in n[1] for n in sent)


# ---- then: the log ----

@then('the log has a line with the key id, "{a}", "{b}" and "{c}"')
def log_line(context, a, b, c):
    lines = [x for x in context.log.lines if f"key_id={context.key_id}" in x and a in x and b in x and c in x]
    assert len(lines) == 1, context.log.lines


@then('no log line holds the key, the owner\'s name or "{word}"')
def log_private(context, word):
    text = "\n".join(context.log.lines)
    assert text and context.key not in text and context.owner not in text and word not in text, text


@then('the log has the line "{line}"')
def log_has_line(context, line):
    pattern = re.escape(line).replace(re.escape("<id>"), "[0-9a-f-]{36}")
    assert any(re.fullmatch(pattern, x) for x in context.log.lines), context.log.lines


# ---- then: the text log ----

@then("the texts table has no item for the key")
def no_texts(context):
    time.sleep(0.3)
    _set = os.environ.get("TABLE_TEXTS")
    os.environ["TABLE_TEXTS"] = "llm-gateway-texts"                # to read the table, whatever the scenario set
    try:
        context.harness.aws.reset()
        assert context.harness.text_rows(context.key_id) == []
    finally:
        if _set is None:
            os.environ.pop("TABLE_TEXTS", None)
        context.harness.aws.reset()


@then("the texts table has one item for the key and the request id of the reply")
def one_text(context):
    rid = context.response.headers["x-request-id"]
    deadline = time.time() + 5
    while time.time() < deadline and not context.harness.text_rows(context.key_id):
        time.sleep(0.05)
    rows = context.harness.text_rows(context.key_id)
    assert [r["sk"] for r in rows] == [rid], rows
    context.text_item = rows[0]


@then('the item holds the request as it went upstream and the reply "{reply}"')
def text_item(context, reply):
    [got] = _received(context)
    assert json.loads(context.text_item["request"]) == got["body"]
    assert json.loads(context.text_item["reply"])["content"] == reply
    assert context.said in context.text_item["request"]


# ---- then: gw-admin output, the function, the template, the README ----

@then('the printed output has a line with the key id, today (UTC), "{rest}"')
def spend_line(context, rest):
    want = f"{context.key_id}  {datetime.now(UTC):%Y-%m-%d}  {rest}"
    assert any(line.strip() == want for line in context.printed.splitlines()), context.printed


@then('the printed output has a line "total", the key id and "{amount}"')
def spend_total(context, amount):
    assert any(line.strip() == f"total {context.key_id}  {amount}" for line in context.printed.splitlines()), \
        context.printed


@then("the function's reserved concurrency is {n:d}")
def reserved_concurrency(context, n):
    got = boto3.client("lambda", region_name="ca-central-1").get_function_concurrency(FunctionName="llm-gateway")
    assert got.get("ReservedConcurrentExecutions") == n, got


@then('no resource of infra/template.yaml has a type that starts with "{prefix}"')
def template_no_iam(context, prefix):
    text = (ROOT / "ext-deploy" / "infra" / "template.yaml").read_text()
    types = re.findall(r"^\s+Type: (AWS::\S+)", text, flags=re.M)
    assert len(types) >= 5 and not [t for t in types if t.startswith(prefix)], types


@then('the template takes the function\'s role as the parameter "{name}"')
def template_role_parameter(context, name):
    text = (ROOT / "ext-deploy" / "infra" / "template.yaml").read_text()
    assert f"  {name}:\n    Type: String" in text and f"Role: !Ref {name}" in text


def _clean_up_block() -> str:
    readme = (ROOT / "ext-deploy" / "README.md").read_text()
    section = readme[readme.index("### Clean up"):]
    return re.search(r"```\n(.*?)```", section, flags=re.S).group(1)


@then('the README\'s clean-up block deletes the stack "{stack}"')
def clean_up_stack(context, stack):
    assert f"sam delete --stack-name {stack} " in _clean_up_block()


@then('it deletes the tables "{tables}"')
def clean_up_tables(context, tables):
    block = _clean_up_block()
    for table in (t.strip() for t in tables.split(",")):
        assert f"aws dynamodb delete-table --region $REGION --table-name {table}" in block, table
    template = (ROOT / "ext-deploy" / "infra" / "template.yaml").read_text()
    assert sorted(set(re.findall(r"TableName: (\S+)", template))) == sorted(t.strip() for t in tables.split(","))


@then('it deletes the secret "{secret}" without a recovery window')
def clean_up_secret(context, secret):
    assert re.search(rf"delete-secret --region \$REGION --secret-id {re.escape(secret)} \\\n\s+--force-delete-without-recovery",
                     _clean_up_block())


@then('it deletes the role "{role}" and its inline policy')
def clean_up_role(context, role):
    block = _clean_up_block()
    assert f"aws iam delete-role-policy --role-name {role} --policy-name {role}" in block
    assert f"aws iam delete-role --role-name {role}" in block


# ---- the reserve's boundary, max_completion_tokens, replies without usage ----

@when("the key sends a chat of {n:d} bytes with max_tokens {m:d}")
def key_chat_bytes(context, n, m):
    _post(context, context_free_chat(max_tokens=m, messages=[{"role": "user", "content": "x" * n}]))


@when("the key sends a chat with max_completion_tokens {n:d} and no max_tokens")
def key_chat_mct(context, n):
    _post(context, context_free_chat(max_completion_tokens=n))


@then("the upstream received a chat with max_completion_tokens {n:d} and no max_tokens")
def upstream_chat_mct(context, n):
    [got] = _received(context)
    assert got["body"].get("max_completion_tokens") == n and "max_tokens" not in got["body"], got["body"]


@when("the key sends a chat with max_tokens {n:d} and the upstream reports no usage")
def key_chat_no_usage(context, n):
    _post(context, context_free_chat(max_tokens=n, x_fake={"no_usage": True}))


@when("the key sends a streamed chat and the upstream sends {chunks:d} chunks and no usage")
def key_stream_no_usage(context, chunks):
    _post(context, context_free_chat(stream=True, x_fake={"chunks": chunks, "no_usage": True}))


@when("the key sends a streamed chat with max_tokens {n:d} and the upstream breaks after {done:d} of {chunks:d} chunks")
def key_stream_breaks(context, n, done, chunks):
    _post(context, context_free_chat(stream=True, max_tokens=n, x_fake={"chunks": chunks, "fail_after": done}))


@when("the key sends a streamed chat with max_tokens {n:d} and the upstream waits {seconds:g} second after each of {chunks:d} chunks")
def key_stream_stalls(context, n, seconds, chunks):
    _post(context, context_free_chat(stream=True, max_tokens=n, x_fake={"chunks": chunks, "delay": seconds}))


@then("the usage row of the key counts {n:d} request as estimated")
def usage_estimated(context, n):
    [row] = context.harness.wait_for_usage(context.key_id)
    assert int(row.get("requests_estimated", 0)) == n, row


@then('it deletes the inline policy "{policy}" that the deploy steps put on the role you work with')
def clean_up_deploy_policy(context, policy):
    assert f"aws iam delete-role-policy --role-name <the role you work with> --policy-name {policy}" in _clean_up_block()
    readme = (ROOT / "ext-deploy" / "README.md").read_text()
    assert f"aws iam put-role-policy --role-name <the role you work with> --policy-name {policy}" in readme


@then('the README says that a table that was never created answers "{error}"')
def clean_up_error(context, error):
    readme = (ROOT / "ext-deploy" / "README.md").read_text()
    section = readme[readme.index("### Clean up"):readme.index("## Known limits")]
    assert error in section


# ---- a gateway that takes a request and does not answer, an answer that is not JSON ----

def _stand_in(context, handle):
    """A server on 127.0.0.1 that records each request line; `handle(handler)` writes the answer or does not."""
    import http.server
    import threading

    seen = context.stand_in_seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def _any(self):
            self.rfile.read(int(self.headers.get("content-length") or 0))
            seen.append(f"{self.command} {self.path}")
            handle(self)

        do_GET = do_POST = _any

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    context.add_cleanup(server.shutdown)
    _set_env(context, "COURSE_GATEWAY_URL", f"http://127.0.0.1:{server.server_address[1]}/v1")
    _set_env(context, "KB_MCP_TOKEN", "a-made-up-token")


def _answer(handler, status, content_type, body: bytes):
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


@given("a stand-in at COURSE_GATEWAY_URL that lists its models and takes a chat without answering it")
def stand_in_silent(context):
    def handle(handler):
        if handler.command == "GET":
            _answer(handler, 200, "application/json", json.dumps({"data": [{"id": "example-small"}]}).encode())
        else:
            time.sleep(1.5)        # longer than the shortened limit; the client has given up by then

    _stand_in(context, handle)


@given("a stand-in at COURSE_GATEWAY_URL that answers every request with a web page")
def stand_in_web_page(context):
    _stand_in(context, lambda handler: _answer(handler, 200, "text/html", b"<html>Sign in</html>"))


@given("the client's time limit is shortened to {seconds:g} seconds")
def client_limit(context, seconds):
    context.client_timeout = seconds


@then("the stand-in received {n:d} chat")
def stand_in_chats(context, n):
    time.sleep(0.8)                # a second attempt would have arrived by now
    assert [line for line in context.stand_in_seen if line.startswith("POST")] == ["POST /v1/chat/completions"] * n, \
        context.stand_in_seen


@then("the stand-in received {n:d} request in all")
def stand_in_all(context, n):
    assert len(context.stand_in_seen) == n, context.stand_in_seen


@then("the client sent {n:d} request")
def client_sent(context, n):
    assert len(context.client_requests) == n, context.client_requests


@when('the client builds the sampling settings for "{model}"')
def build_sampling(context, model):
    context.sampling = client_mod.sampling(model)


@then("they are {settings}")
def sampling_is(context, settings):
    assert context.sampling == json.loads(settings), context.sampling

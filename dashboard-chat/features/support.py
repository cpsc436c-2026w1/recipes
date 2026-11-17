"""Stand-ins and helpers the step files share. None of them opens a network connection.

The LLM stand-ins have the one method the recipe's models have, `chat(messages, tools) -> ModelReply`
(main/model.py), so main/chat.py cannot tell them from the gateway client.
"""

from __future__ import annotations

import contextlib
import io
import json

import httpx

from main.config import Settings
from main.model import GatewayModel, ModelReply, ToolCall

MODEL = "qwen3.6-35b-a3b"
GATEWAY_URL = "https://gw.example/v1"   # a made-up address: the stand-in's transport never leaves the process
TOKEN = "tok-123"                        # a made-up course token


class Recording:
    """Wraps an LLM stand-in and keeps the messages of every call, so a step can read what the LLM received."""

    def __init__(self, inner):
        self.inner, self.calls = inner, []
        self.model = getattr(inner, "model", MODEL)

    def chat(self, messages, tools=None):
        self.calls.append([dict(m) for m in messages])
        return self.inner.chat(messages, tools)

    def tool_results(self) -> list[dict]:
        """Every tool result the LLM received, in order, parsed from JSON."""
        last = self.calls[-1] if self.calls else []
        return [json.loads(m["content"]) for m in last if m["role"] == "tool"]


class Scripted:
    """An LLM that does what the script says, call by call; the last entry repeats.

    ("tool", name, sql) is a tool call, ("reply", text) a reply, ("fail", message) a failed model call."""

    model = MODEL

    def __init__(self, *script):
        self.script, self.n = script, 0

    def chat(self, messages, tools=None):
        action = self.script[min(self.n, len(self.script) - 1)]
        self.n += 1
        if action[0] == "tool":
            return ModelReply(None, [ToolCall(f"call-{self.n}", action[1], {"sql": action[2]})], 10, 2, self.model)
        if action[0] == "fail":
            raise RuntimeError(f"course LLM gateway refused the call: {action[1]}")
        return ModelReply(action[1], [], 10, 2, self.model)


class FailsSomeQuestions:
    """The inner stand-in, except that the model call fails for the named questions (a refusal by the gateway)."""

    def __init__(self, inner, failing: dict[str, str]):
        self.inner, self.failing, self.model = inner, failing, getattr(inner, "model", MODEL)

    def chat(self, messages, tools=None):
        question = next(m["content"] for m in reversed(messages) if m["role"] == "user")
        if question in self.failing:
            raise RuntimeError(f"course LLM gateway refused the call: {self.failing[question]}")
        return self.inner.chat(messages, tools)


def gateway_stand_in(status: int, requests: list) -> GatewayModel:
    """The recipe's own gateway client (main/model.py), with its HTTP transport replaced by one that answers
    here. The address, the headers and the timeout are the ones the client built, so a scenario sees what the
    recipe would send. 200 answers with a reply; any other status answers with an error body."""

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if status == 200:
            return httpx.Response(200, json={
                "model": json.loads(request.content)["model"],
                "choices": [{"message": {"content": "There are 1200 orders."}}],
                "usage": {"prompt_tokens": 120, "completion_tokens": 9}})
        return httpx.Response(status, json={"error": {"message": "refused by the stand-in", "code": f"code-{status}"}})

    model = GatewayModel(GATEWAY_URL, TOKEN, Settings().chat_model)
    built = model.http
    model.http = httpx.Client(base_url=built.base_url, headers=built.headers, timeout=built.timeout,
                              transport=httpx.MockTransport(handler))
    return model


def gateway_that_uses_its_cap(requests: list, query_first: bool) -> GatewayModel:
    """The recipe's gateway client over a stand-in whose reply stops at max_tokens before any text: content "",
    finish_reason "length", every completion token spent (as qwen3.8-27b replied with a cap of 150, in
    ../../hybrid-retrieval-eval/output/routing-gateway-150-cap.txt). With `query_first`, the first call asks for a
    query and the second is the empty one."""

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = json.loads(request.content)
        if query_first and len(requests) == 1:
            call = {"id": "call-1", "type": "function",
                    "function": {"name": "run_sql", "arguments": json.dumps({"sql": "SELECT COUNT(*) FROM orders"})}}
            return httpx.Response(200, json={
                "model": body["model"],
                "choices": [{"message": {"content": None, "tool_calls": [call]}, "finish_reason": "tool_calls"}],
                "usage": {"prompt_tokens": 120, "completion_tokens": 30}})
        return httpx.Response(200, json={
            "model": body["model"],
            "choices": [{"message": {"content": ""}, "finish_reason": "length"}],
            "usage": {"prompt_tokens": 150, "completion_tokens": body["max_tokens"]}})

    return over_stand_in(GatewayModel(GATEWAY_URL, TOKEN, Settings().chat_model), handler)


def gateway_that_cuts_its_tool_call(requests: list) -> GatewayModel:
    """The recipe's gateway client over a stand-in whose tool call stops at max_tokens in the middle of its
    arguments: finish_reason "length", arguments that are not whole JSON."""

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = json.loads(request.content)
        call = {"id": "call-1", "type": "function",
                "function": {"name": "run_sql", "arguments": '{"sql": "SELECT COUNT(*) FR'}}
        return httpx.Response(200, json={
            "model": body["model"],
            "choices": [{"message": {"content": None, "tool_calls": [call]}, "finish_reason": "length"}],
            "usage": {"prompt_tokens": 150, "completion_tokens": body["max_tokens"]}})

    return over_stand_in(GatewayModel(GATEWAY_URL, TOKEN, Settings().chat_model), handler)


def gateway_that_times_out(requests: list) -> GatewayModel:
    """The recipe's gateway client over a transport that raises what httpx raises when the time limit of a call
    passes without an answer (httpx.ReadTimeout). No time passes in the scenario."""

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise httpx.ReadTimeout("the stand-in did not answer", request=request)

    model = GatewayModel(GATEWAY_URL, TOKEN, Settings().chat_model)
    built = model.http
    model.http = httpx.Client(base_url=built.base_url, headers=built.headers, timeout=built.timeout,
                              transport=httpx.MockTransport(handler))
    return model


def over_stand_in(model: GatewayModel, handler) -> GatewayModel:
    """Replace the HTTP transport of a gateway client with `handler`; keep the address, headers and timeout."""
    built = model.http
    model.http = httpx.Client(base_url=built.base_url, headers=built.headers, timeout=built.timeout,
                              transport=httpx.MockTransport(handler))
    return model


def refuses_first_call(code: str, retry_after: str | None, requests: list):
    """A gateway that refuses the first call with HTTP 429, the given error code and Retry-After header (None:
    no header), as the course gateway does (recipes/llm-gateway/ext-deploy/src/gateway/budget.py, upstream.py),
    and answers every later call."""

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            headers = {} if retry_after is None else {"Retry-After": retry_after}
            return httpx.Response(429, headers=headers,
                                  json={"error": {"message": "refused by the stand-in", "code": code}})
        return httpx.Response(200, json={
            "model": json.loads(request.content)["model"],
            "choices": [{"message": {"content": "I can only answer questions about the shop's sales."}}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 9}})

    return handler


def run_captured(context, fn, *args, **kwargs):
    """Call fn, keep what it printed in context.printed / context.printed_err and its exit code in
    context.exit_code (0 when it returned; a SystemExit with a message is exit code 1, as in a shell)."""
    out, err = io.StringIO(), io.StringIO()
    result, context.exit_code = None, 0
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            result = fn(*args, **kwargs)
        except SystemExit as e:
            if e.code is None or isinstance(e.code, int):
                context.exit_code = e.code or 0
            else:
                context.exit_code = 1
                err.write(str(e.code) + "\n")
    context.printed += out.getvalue()
    context.printed_err += err.getvalue()
    return result


def set_env(context, name: str, value: str | None) -> None:
    """Set (or with None, remove) an environment variable for this scenario only."""
    import os

    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value


def patch(context, obj, name: str, value) -> None:
    """Replace an attribute for this scenario only."""
    old = getattr(obj, name)
    setattr(obj, name, value)
    context.add_cleanup(setattr, obj, name, old)


def in_moto(context) -> None:
    assert context.moto is not None, "this step calls AWS: tag the scenario @aws so it runs inside moto"


def inventory(region: str) -> dict:
    """Everything of the kinds this recipe creates, whatever its name, in the (moto) account."""
    import boto3

    sns = boto3.client("sns", region_name=region)
    return {
        "tables": sorted(boto3.client("dynamodb", region_name=region).list_tables()["TableNames"]),
        "alarms": sorted(a["AlarmName"] for a in
                         boto3.client("cloudwatch", region_name=region).describe_alarms()["MetricAlarms"]),
        "topics": sorted(t["TopicArn"].split(":")[-1] for t in sns.list_topics()["Topics"]),
        "subscriptions": sorted(s["Endpoint"] for s in sns.list_subscriptions()["Subscriptions"]),
    }


def real_run_set_up(s: Settings) -> None:
    """What main/run.py, cache_run.py and trace_run.py --email create in a run with --target aws."""
    from alarms import ensure_alarms

    from main.infra import ensure_table

    for name, key, sort_key in ((s.traces_table, "request_id", None), (s.cache_table, "cache_key", None),
                                (s.spans_table, "trace_id", "span_id")):
        ensure_table(name, s.region, key=key, sort_key=sort_key)
    ensure_alarms(s.alarm_prefix, s.metric_namespace, s.topic_name, s.region, email="ana@example.com")

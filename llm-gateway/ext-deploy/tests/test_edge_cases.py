"""Edge cases: the caps a request cannot get around, replies without usage, bad admin input,
and the shape of the template and the IAM policies."""

import json
import os
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

import conftest
import fake_upstream
from conftest import bearer, chat_body, key_item, wait_for_usage
from gateway import budget, pricing

CHAT = "/v1/chat/completions"
INFRA = Path(__file__).resolve().parents[1] / "infra"


async def _json_always(request):
    """An upstream that answers one JSON body even when asked to stream."""
    body = json.loads(await request.body())
    fake = body.get("x_fake", {})
    reply = {"choices": [{"message": {"role": "assistant", "content": "x"}}]}
    if not fake.get("no_usage"):
        reply["usage"] = {"prompt_tokens": 18, "completion_tokens": fake.get("completion_tokens", 100)}
    return JSONResponse(reply)


@pytest.fixture
def json_upstream(gateway_url):
    saved = os.environ["UPSTREAM_BASE_URL"]
    server, thread, url = conftest.start_server(
        Starlette(routes=[Route("/v1/chat/completions", _json_always, methods=["POST"])]))
    os.environ["UPSTREAM_BASE_URL"] = url + "/v1"
    yield url
    os.environ["UPSTREAM_BASE_URL"] = saved
    server.should_exit = True
    thread.join(5)


def test_output_bound_is_the_larger_cap():
    assert budget.output_tokens_bound({"max_tokens": 1, "max_completion_tokens": 8192}) == 8192


def test_mismatched_caps_stay_inside_budget(client, issue, json_upstream):
    key_id, key = issue(owner="caps", budget="0.001")
    body = chat_body(max_tokens=1, max_completion_tokens=8192, x_fake={"completion_tokens": 8192})
    codes = [client.post(CHAT, json=body, headers=bearer(key)).status_code for _ in range(3)]
    item = key_item(key_id)
    assert codes == [429, 429, 429]
    assert int(item["spent_micro"]) <= int(item["budget_micro"])


@pytest.mark.parametrize("val", ["no", "true", 0, 1, [], {}])
def test_stream_must_be_boolean(client, issue, val):
    _, key = issue(owner="streamtype")
    r = client.post(CHAT, json=chat_body(stream=val), headers=bearer(key))
    assert r.status_code == 400 and r.json()["error"]["code"] == "bad_stream"


def test_stream_answered_with_json_is_settled_as_buffered(client, issue, json_upstream):
    key_id, key = issue(owner="jsonstream", budget="3")
    r = client.post(CHAT, json=chat_body(stream=True, x_fake={"completion_tokens": 8192}), headers=bearer(key))
    assert r.status_code == 200
    [row] = wait_for_usage(key_id)
    assert int(row["tokens_out"]) == 8192


def test_buffered_reply_without_usage_keeps_the_reserve(client, issue, json_upstream):
    key_id, key = issue(owner="nousage2", budget="3")
    r = client.post(CHAT, json=chat_body(max_tokens=8192, x_fake={"no_usage": True}), headers=bearer(key))
    assert r.status_code == 200
    [row] = wait_for_usage(key_id)
    assert int(row["cost_micro"]) >= pricing.cost_out("example-large", 8192)
    assert int(row["requests_estimated"]) == 1


@pytest.mark.parametrize("messages", [5, "hi", {"role": "user"}, None])
def test_messages_must_be_a_list(client, issue, messages):
    key_id, key = issue(owner="msgs")
    r = client.post(CHAT, json=chat_body(messages=messages), headers=bearer(key))
    assert r.status_code == 400 and r.json()["error"]["code"] == "bad_messages"
    assert int(key_item(key_id)["spent_micro"]) == 0


def test_estimate_refuses_non_list_messages():
    with pytest.raises(budget.GatewayError):
        budget.input_tokens_estimate({"messages": 5}, embeddings=False)


@pytest.mark.parametrize("field", ["max_tokens", "max_completion_tokens", "n"])
def test_boolean_is_not_an_integer(client, issue, field):
    _, key = issue(owner="booly")
    r = client.post(CHAT, json=chat_body(**{field: True}), headers=bearer(key))
    assert r.status_code == 400


def test_admin_issue_refuses_bad_input(issue):
    for argv in (["--models", ""], ["--models", ","], ["--expires", "2020-01-01"], ["--expires", "soon"],
                 ["--budget", "abc"], ["--budget", "-1"], ["--budget", "NaN"]):
        with pytest.raises(SystemExit) as e:
            conftest.run_admin("issue", "--owner", "bad", "--kind", "person", *argv)
        assert isinstance(e.value.code, str), argv  # a message, not a traceback


def test_function_url_only_for_function_url_transport():
    text = (INFRA / "template.yaml").read_text()
    assert "FunctionUrlConfig" not in text
    for name in ("GatewayFunctionUrl:", "GatewayFunctionUrlPermission:", "GatewayFunctionUrlInvokePermission:"):
        block = text.split(name, 1)[1].split("\n\n", 1)[0]
        assert "Condition: UseFunctionUrl" in block


def test_iam_policies_are_narrow():
    fn = json.loads((INFRA / "iam/function-role-policy.json").read_text())
    assert not any("index/" in r for s in fn["Statement"] for r in s.get("Resource", []) if isinstance(r, str))
    admin = json.loads((INFRA / "iam/deploy-admin-policy.json").read_text())
    assert not any("apigateway" in json.dumps(s.get("Action")) for s in admin["Statement"])
    addon = json.loads((INFRA / "iam/deploy-buffered-addon-policy.json").read_text())
    assert any("apigateway" in json.dumps(s.get("Action")) for s in addon["Statement"])


def test_input_estimate_counts_text_outside_content():
    # An assistant message whose text is in tool_calls, as a client sends it back after a tool call.
    args = "x" * 10_000
    call = {"role": "assistant", "content": None,
            "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "f", "arguments": args}}]}
    plain = budget.input_tokens_estimate({"messages": [{"role": "user", "content": "hi"}]}, embeddings=False)
    assert plain == 2 + 16                                       # unchanged for a message with role and content only
    with_call = budget.input_tokens_estimate({"messages": [{"role": "user", "content": "hi"}, call]}, embeddings=False)
    assert with_call >= plain + 10_000                           # an upper bound: at least one token per byte

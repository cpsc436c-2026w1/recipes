"""The tools behind the Gateway, offline: the tool names carry the target's name, and the same hooks apply."""
import json

import boto3
import httpx
import pytest

from main.agent import run_request
from main.contracts import TOOLS
from main.gateway_tools import GatewayLibrary, GatewaySigV4
from main.invoke import SESSION_ID_CHARS, ask_person, conversation
from main.model import StubModel
from main.request_key import request_key
from tests.fakes import FakeGatewayClient

REQUEST = "Which is shorter, Dune or Emma? Place a hold on the shorter one for me."


@pytest.fixture
def behind_gateway(library):
    client = FakeGatewayClient(library, extra=("other-target___get_book", "other-target___run_query"))
    return GatewayLibrary(client), client


def test_only_this_targets_tools_are_given_to_the_agent(behind_gateway):
    gateway, _ = behind_gateway
    assert sorted(t.tool_name for t in gateway.tools) == sorted(f"library-tools___{name}" for name in TOOLS)


def test_a_gateway_without_the_target_is_named_in_the_error(library):
    with pytest.raises(RuntimeError) as err:
        GatewayLibrary(FakeGatewayClient(library, target="something-else"))
    assert "library-tools" in str(err.value)


def test_the_hooks_apply_to_the_gateways_tool_names(s, behind_gateway, library):
    gateway, client = behind_gateway
    run = run_request(s, REQUEST, "sam", StubModel("wrong_member"), gateway, lambda a: True, request_id="req-1",
                      tools=gateway.tools)
    assert run["status"] == "done" and run["tools_ran"] == ["search_books", "search_books", "place_hold"]
    assert run["approvals_asked"][0]["title"] == "Emma"                 # the hook read the book through the Gateway
    write = [args for name, args in client.calls if name == "place_hold"][0]
    assert write == {"member": "sam", "book_id": "b02", "request_id": request_key("req-1", "sam", "b02")}
    assert library.get_holds("alex")["holds"] == []


def test_the_caps_apply_to_the_gateways_tools(s, behind_gateway):
    gateway, client = behind_gateway
    run = run_request(s, REQUEST, "sam", StubModel("runaway"), gateway, lambda a: True, tools=gateway.tools)
    assert run["status"] == "stopped" and len([c for c in client.calls if c[0] == "search_books"]) == s.max_tool_calls


def test_a_tool_result_with_another_shape_is_not_passed_on(library):
    client = FakeGatewayClient(library)
    library.call = lambda name, args: {"title": "Dune"}                  # a get_book result without its other fields
    with pytest.raises(ValueError):
        GatewayLibrary(client).get_book("b01")


def test_every_request_to_the_gateway_is_signed_for_bedrock_agentcore():
    session = boto3.Session(aws_access_key_id="testing", aws_secret_access_key="testing", region_name="ca-central-1")
    request = httpx.Request("POST", "https://gateway.test.invalid/mcp", json={"method": "tools/list"})
    signed = next(GatewaySigV4(session, "ca-central-1").auth_flow(request))
    assert "/ca-central-1/bedrock-agentcore/aws4_request" in signed.headers["authorization"]
    assert "x-amz-date" in signed.headers and len(signed.headers["x-amz-content-sha256"]) == 64


class FakeRuntimeClient:
    """A stand-in for the bedrock-agentcore data client: answers like a Runtime whose agent waits once."""

    def __init__(self):
        self.calls = []

    def invoke_agent_runtime(self, **kw):
        payload = json.loads(kw["payload"])
        self.calls.append({**kw, "payload": payload})
        body = ({"status": "needs_approval", "approvals": [{"id": "a1", "title": "Emma", "author": "Jane Austen", "member": "sam"}]}
                if payload["mode"] == "request" else {"status": "done", "answer": payload["answer"]})
        return {"response": type("Body", (), {"read": lambda self: json.dumps(body).encode()})()}


def test_the_request_and_its_approval_go_to_one_runtime_session(s):
    client = FakeRuntimeClient()
    replies = conversation(s, client, "arn:aws:bedrock-agentcore:ca-central-1:123456789012:runtime/test_agent-0000000001",
                           REQUEST, "sam", lambda reply: "y")
    assert [r["status"] for r in replies] == ["needs_approval", "done"]
    first, second = client.calls
    assert first["runtimeSessionId"] == second["runtimeSessionId"] and len(first["runtimeSessionId"]) >= SESSION_ID_CHARS
    assert first["payload"] == {"mode": "request", "prompt": REQUEST, "member": "sam"}
    assert second["payload"] == {"mode": "approve", "answer": "y"}


@pytest.mark.parametrize("no_answer", [EOFError, KeyboardInterrupt])
def test_no_answer_at_the_runtime_approval_prompt_is_a_no(s, monkeypatch, no_answer):
    def no_input(prompt):
        raise no_answer()
    monkeypatch.setattr("builtins.input", no_input)
    client = FakeRuntimeClient()
    replies = conversation(s, client, "arn:aws:bedrock-agentcore:ca-central-1:123456789012:runtime/test_agent-0000000001",
                           REQUEST, "sam", ask_person)
    assert client.calls[1]["payload"] == {"mode": "approve", "answer": "n"} and replies[-1]["answer"] == "n"

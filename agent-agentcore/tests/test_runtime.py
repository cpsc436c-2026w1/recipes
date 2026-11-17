"""What the Runtime's entry point does with a payload, offline: main/session.py on the stub model, and
runtime/agent_entry.py itself with stand-ins for the AgentCore SDK, the Gateway connection and the model."""
import importlib.util
import sys
import types

import pytest

from main.model import StubModel
from main.session import Sessions
from main.store import MemoryStore
from main.tools import LibraryTools
from tests.conftest import ROOT

REQUEST = {"mode": "request", "prompt": "Which is shorter, Dune or Emma? Place a hold on the shorter one for me.",
           "member": "sam"}


@pytest.fixture
def sessions(s, library):
    return Sessions(s, StubModel(), library)


def test_a_request_with_a_write_waits_for_the_approval(sessions, library):
    out = sessions.handle(REQUEST, "session-a")
    assert out["status"] == "needs_approval" and out["approvals"][0]["title"] == "Emma"
    assert out["runtime_session"] == "session-a" and library.get_holds("sam")["holds"] == []


def test_an_approval_in_the_same_session_resumes_the_waiting_agent(sessions, library):
    first = sessions.handle(REQUEST, "session-a")
    done = sessions.handle({"mode": "approve", "answer": "y"}, "session-a")
    assert done["status"] == "done" and done["request_id"] == first["request_id"]
    assert done["model_calls"] == 4                                       # the count went on: one request, two calls
    assert done["holds"] == {"member": "sam", "holds": [{"book_id": "b02", "title": "Emma"}]}
    assert sessions.waiting == {}


def test_a_declined_approval_places_no_hold(sessions):
    sessions.handle(REQUEST, "session-a")
    done = sessions.handle({"mode": "approve", "answer": "n"}, "session-a")
    assert done["status"] == "done" and done["holds"]["holds"] == []


def test_an_approval_in_another_session_finds_nothing_waiting(sessions, library):
    sessions.handle(REQUEST, "session-a")
    other = sessions.handle({"mode": "approve", "answer": "y"}, "session-b")
    assert other == {"status": "error", "error": "no approval is waiting in this session"}
    assert library.get_holds("sam")["holds"] == [] and "session-a" in sessions.waiting


@pytest.mark.parametrize("payload, says", [
    ({"mode": "request", "prompt": "hello"}, "needs a prompt and a member"),
    ({"mode": "delete-everything"}, "unknown mode"),
])
def test_a_payload_the_entry_point_does_not_know_is_an_error_result(sessions, payload, says):
    assert says in sessions.handle(payload, "session-a")["error"]


def test_a_read_only_request_finishes_in_one_call(sessions):
    out = sessions.handle({"mode": "request", "prompt": "Which is shorter, Frankenstein or Dracula?", "member": "kim"}, "s1")
    assert out["status"] == "done" and "Frankenstein" in out["reply"] and out["holds"]["holds"] == []


# ---- runtime/agent_entry.py ------------------------------------------------------------------------------------------
@pytest.fixture
def runtime_main(aws, monkeypatch):
    """runtime/agent_entry.py loaded with: a stand-in for bedrock_agentcore (it is installed only in the Runtime's zip),
    the secrets in moto's Parameter Store, local tools in place of the Gateway, the stub in place of the model."""
    class App:
        def entrypoint(self, fn):
            self.handler = fn
            return fn

        def run(self):
            raise AssertionError("not started in a test")

    fake = types.ModuleType("bedrock_agentcore.runtime")
    fake.BedrockAgentCoreApp = App
    monkeypatch.setitem(sys.modules, "bedrock_agentcore", types.ModuleType("bedrock_agentcore"))
    monkeypatch.setitem(sys.modules, "bedrock_agentcore.runtime", fake)
    aws.ssm.put_parameter(Name="test-agent-kb-mcp-token", Value="made-up-course-token\n", Type="SecureString")
    monkeypatch.delenv("KB_MCP_TOKEN")
    monkeypatch.delenv("LOGFIRE_TOKEN")
    monkeypatch.setenv("KB_MCP_TOKEN_PARAM", "test-agent-kb-mcp-token")
    monkeypatch.setenv("LOGFIRE_TOKEN_PARAM", "")
    monkeypatch.setenv("TOOLS_GATEWAY_URL", "https://gateway.test.invalid/mcp")
    seen = {}

    class LocalInPlaceOfGateway(LibraryTools):
        def __init__(self, client):
            super().__init__(MemoryStore())
            seen["client"] = client
            self.tools = None            # None: the agent builds the local Strands tools

    def model(settings):
        seen["token"] = settings.kb_mcp_token
        return StubModel()

    monkeypatch.setattr("main.gateway_tools.open_gateway", lambda url, region: ("client for", url, region))
    monkeypatch.setattr("main.gateway_tools.GatewayLibrary", LocalInPlaceOfGateway)
    monkeypatch.setattr("main.model.gateway_model", model)
    spec = importlib.util.spec_from_file_location("agent_entry", ROOT / "runtime" / "agent_entry.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, seen


def test_the_entry_point_reads_the_token_from_parameter_store_and_answers_in_sessions(runtime_main):
    module, seen = runtime_main
    context = types.SimpleNamespace(session_id="session-a")
    first = module.invoke(REQUEST, context)
    assert first["status"] == "needs_approval"
    assert seen["token"] == "made-up-course-token"                       # read with the Runtime's role, at first use
    assert seen["client"] == ("client for", "https://gateway.test.invalid/mcp", "ca-central-1")
    done = module.invoke({"mode": "approve", "answer": "y"}, context)
    assert done["status"] == "done" and done["holds"]["holds"] == [{"book_id": "b02", "title": "Emma"}]
    assert module.app.handler is module.invoke

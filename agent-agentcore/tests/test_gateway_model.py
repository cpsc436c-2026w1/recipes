"""--model gateway, offline: the agent's real OpenAI-compatible client against a local stand-in for the course
LLM gateway. Shows what leaves this machine in a model call, and that the caps hold for a real client too."""
import pytest

from main.agent import run_request
from main.config import load_settings
from main.contracts import TOOLS
from main.model import gateway_model, sampling
from tests.fake_gateway import FakeGateway, tool_call


@pytest.fixture
def gateway(monkeypatch):
    fake = FakeGateway([tool_call("search_books", query="Emma"), tool_call("place_hold", member="sam", book_id="b02"),
                        "I placed a hold on Emma for you."])
    monkeypatch.setenv("COURSE_GATEWAY_URL", fake.url)
    monkeypatch.setenv("KB_MCP_TOKEN", "made-up-course-token")
    yield fake
    fake.close()


def test_the_model_calls_go_to_the_gateway_with_the_course_token(gateway, library):
    s = load_settings()
    run = run_request(s, "Place a hold on Emma for me.", "sam", gateway_model(s), library, lambda a: True)
    assert run["status"] == "done" and run["reply"] == "I placed a hold on Emma for you."
    assert [h["book_id"] for h in library.get_holds("sam")["holds"]] == ["b02"]
    assert len(gateway.requests) == run["model_calls"] == 3
    first = gateway.requests[0]
    assert first["path"] == "/v1/chat/completions" and first["authorization"] == "Bearer made-up-course-token"
    assert first["body"]["model"] == s.agent_model and first["body"]["max_tokens"] == s.reply_max_tokens
    assert first["body"]["temperature"] == 0
    assert {t["function"]["name"] for t in first["body"]["tools"]} == set(TOOLS)
    assert first["body"]["messages"][0]["role"] == "system"


def test_the_cap_ends_a_gateway_model_that_never_stops(gateway, library, monkeypatch):
    gateway.script = [tool_call("search_books", query="novel")]            # the same tool call, for ever
    monkeypatch.setenv("MAX_MODEL_CALLS", "3")
    s = load_settings()
    run = run_request(s, "Find me a novel.", "sam", gateway_model(s), library, lambda a: True)
    assert run["status"] == "stopped" and len(gateway.requests) == 3         # the fourth request was not sent


@pytest.mark.parametrize("empty, named", [("KB_MCP_TOKEN", "KB_MCP_TOKEN"), ("COURSE_GATEWAY_URL", "COURSE_GATEWAY_URL")])
def test_an_empty_token_or_address_stops_before_any_model_call(gateway, monkeypatch, empty, named):
    monkeypatch.setenv(empty, "")
    with pytest.raises(SystemExit) as stop:
        gateway_model(load_settings())
    assert f"Set {named} in .env" in str(stop.value) and gateway.requests == []


@pytest.mark.parametrize("status", [429, 500])
def test_a_refused_model_call_ends_the_request_after_one_request_to_the_gateway(gateway, library, status):
    """The cap counts model calls. It bounds the requests to the gateway only when one model call is one request,
    so neither the OpenAI client nor Strands sends a refused call again."""
    gateway.status = status
    s = load_settings()
    run = run_request(s, "Place a hold on Emma for me.", "sam", gateway_model(s), library, lambda a: True)
    assert run["status"] == "error" and str(status) in run["error"]
    assert len(gateway.requests) == run["model_calls"] == 1
    assert library.get_holds("sam")["holds"] == []


def test_no_temperature_for_the_models_that_refuse_zero():
    assert sampling("qwen3.8-27b") == {"temperature": 0} and sampling("gpt-6-luna") == {}

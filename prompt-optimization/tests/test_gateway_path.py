"""The gateway path, against a local stand-in: the request goes to <GATEWAY_URL>/chat/completions with the token.

No real endpoint is called. The stand-in answers like an OpenAI-compatible server and records what it received.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from main import models
from main.models import Budget, ModelCallFailed, gateway_chat, make_lm
from main.program import SQLAnswerer
from main.report import program_answer


class Seen(list):
    """The requests the stand-in received; `reply` sets what it answers."""


@pytest.fixture
def fake_gateway(monkeypatch):
    seen = Seen()
    reply = {"text": "[[ ## sql ## ]]\nSELECT COUNT(*) FROM orders\n\n[[ ## completed ## ]]", "finish": "stop"}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
            text = reply["text"]
            out = json.dumps({"id": "1", "object": "chat.completion", "created": 0, "model": body["model"],
                              "choices": [{"index": 0, "finish_reason": reply["finish"],
                                           "message": {"role": "assistant", "content": text}}],
                              "usage": {"prompt_tokens": 1000, "completion_tokens": 100, "total_tokens": 1100}})
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out.encode())

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("COURSE_GATEWAY_URL", f"http://127.0.0.1:{server.server_port}/v1")
    monkeypatch.setenv("KB_MCP_TOKEN", "test-token-not-real")
    seen.reply = reply
    yield seen
    server.shutdown()


def test_dspy_program_calls_the_gateway(fake_gateway):
    budget = Budget()
    lm = make_lm("qwen3.6-35b-a3b", "task", budget, offline=False)
    sql = program_answer(SQLAnswerer(), lm)("How many orders were placed?")
    assert sql == "SELECT COUNT(*) FROM orders"
    req = fake_gateway[0]
    assert req["path"] == "/v1/chat/completions"
    assert req["auth"] == "Bearer test-token-not-real"
    assert req["body"]["model"] == "qwen3.6-35b-a3b"
    assert req["body"]["messages"][0]["role"] == "system"
    assert budget.calls == {"task": 1}
    assert budget.tokens["task"] == [1000, 100]
    assert budget.cad["task"] == pytest.approx((1000 * 0.05 + 100 * 0.10) / 1e6)


def test_plain_chat_calls_the_gateway(fake_gateway):
    budget = Budget()
    reply = gateway_chat("qwen3.8-27b", "", "a reflection prompt", "reflect", budget)
    assert "SELECT COUNT(*)" in reply
    req = fake_gateway[0]
    assert req["path"] == "/v1/chat/completions" and req["auth"] == "Bearer test-token-not-real"
    assert [m["role"] for m in req["body"]["messages"]] == ["user"]
    assert budget.calls == {"reflect": 1}


def test_missing_settings_stop_before_any_call():
    with pytest.raises(SystemExit, match="GATEWAY_URL"):
        make_lm("qwen3.6-35b-a3b", "task", Budget(), offline=False)


def test_each_role_has_its_own_output_cap(fake_gateway):
    # a thinking reflection model needs room to reason before it writes the instruction text
    gateway_chat("qwen3.8-27b", "", "a reflection prompt", "reflect", Budget())
    gateway_chat("qwen3.6-35b-a3b", "", "a question", "task", Budget())
    assert [r["body"]["max_tokens"] for r in fake_gateway] == [models.REFLECTION_MAX_TOKENS, models.TASK_MAX_TOKENS]
    assert models.REFLECTION_MAX_TOKENS == 16384 and models.TASK_MAX_TOKENS < models.REFLECTION_MAX_TOKENS


def test_the_reserve_holds_the_reflection_cap():
    b = Budget()
    held = b.reserve("reflect", "qwen3.8-27b", 1000)
    assert held == pytest.approx((1000 * 0.10 + models.REFLECTION_MAX_TOKENS * 0.20) / 1e6)


@pytest.mark.parametrize("path", ["plain", "dspy"])
def test_an_empty_reflection_reply_is_a_failed_call(fake_gateway, path):
    fake_gateway.reply.update(text="", finish="length")
    budget = Budget()
    with pytest.raises(ModelCallFailed, match="empty reply"):
        if path == "plain":
            gateway_chat("qwen3.8-27b", "", "a reflection prompt", "reflect", budget)
        else:
            make_lm("qwen3.8-27b", "reflect", budget, offline=False)("a reflection prompt")
    assert "REFLECTION_MAX_TOKENS" in budget.failed and budget.stop() is True
    assert budget.tokens["reflect"] == [1000, 100]   # the tokens were spent all the same


def test_gpt_models_get_temperature_1_and_chat_completions(fake_gateway):
    lm = models.GatewayLM("gpt-4o", "reflect", Budget())
    assert lm.model_type == "chat" and lm.kwargs["temperature"] == 1.0
    assert models.GatewayLM("qwen3.8-27b", "reflect", Budget()).kwargs.get("temperature") is None
    models.PRICES_CAD.setdefault("gpt-4o", (0.0, 0.0))
    try:
        gateway_chat("gpt-4o", "", "a reflection prompt", "reflect", Budget())
    finally:
        models.PRICES_CAD.pop("gpt-4o")
    assert fake_gateway[-1]["body"]["temperature"] == 1.0


def test_kb_mcp_token_wins_and_the_older_names_still_work(monkeypatch):
    monkeypatch.setenv("GATEWAY_URL", "http://old.example/v1")
    monkeypatch.setenv("COURSE_MCP_TOKEN", "old-token")
    assert models.gateway_settings() == ("http://old.example/v1", "old-token")
    monkeypatch.setenv("COURSE_GATEWAY_URL", "http://new.example/v1")
    monkeypatch.setenv("KB_MCP_TOKEN", "new-token")
    assert models.gateway_settings() == ("http://new.example/v1", "new-token")

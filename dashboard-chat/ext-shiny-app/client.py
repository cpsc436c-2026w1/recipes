"""The LLM client of the page: the course LLM gateway, or a stand-in on this machine for offline runs.

Decision (model calls): the same as in the command-line recipe. Every call goes to {COURSE_GATEWAY_URL}/chat/completions
with your course MCP token as the bearer key; the token comes from the environment and is never printed. The gateway
speaks the OpenAI chat-completions format (recipes/llm-gateway), so chatlas's client for that format is pointed at
it. querychat takes a chatlas client (https://posit-dev.github.io/chatlas/).

Decision (bounded work): querychat, not main/chat.py, runs the loop of model calls in the page, so the limit of
MAX_MODEL_CALLS per question is kept here, where every request passes: StepLimit below.
"""

from __future__ import annotations

import json

import chatlas
import httpx
from chatformat import completion

from main.chat import MAX_MODEL_CALLS, STEP_LIMIT_REPLY
from main.config import Settings
from main.model import max_tokens_for, sampling



def model_calls_so_far(messages: list[dict]) -> int:
    """How many model calls the question in flight has had: the assistant messages after the last user message."""
    last_user = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=-1)
    return sum(m.get("role") == "assistant" for m in messages[last_user + 1:])


class StepLimit(httpx.AsyncBaseTransport):
    """Sends a request on, unless the question it belongs to has had its MAX_MODEL_CALLS model calls: then it
    answers itself, with the recipe's step-limit reply, and the gateway is not called.

    It keeps no count of its own. It reads the count from the request, which holds the whole conversation, so it
    works the same for every browser session and for every copy querychat makes of the client."""

    def __init__(self, inner: httpx.AsyncBaseTransport | None = None):
        self.inner = inner or httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            body = json.loads(await request.aread() or b"{}")
            if model_calls_so_far(body.get("messages", [])) >= MAX_MODEL_CALLS:
                content_type, data = completion({"content": STEP_LIMIT_REPLY}, body.get("model", ""),
                                                bool(body.get("stream")))
                return httpx.Response(200, headers={"Content-Type": content_type}, content=data, request=request)
        return await self.inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self.inner.aclose()


def make_client(settings: Settings, mode: str = "gateway", table: str = "orders") -> chatlas.Chat:
    """mode "gateway": the course LLM gateway. mode "stub": stub_gateway.py on 127.0.0.1, which calls no model.

    The step limit is on the client's asynchronous calls, the ones a Shiny page makes (chat_async, stream_async).
    """
    if mode == "stub":
        import stub_gateway

        base_url, _server = stub_gateway.start(table)
        token = "stub"  # the stand-in checks no key
    else:
        if not settings.gateway_url or not settings.token:
            raise SystemExit("set COURSE_GATEWAY_URL and KB_MCP_TOKEN first, or APP_MODEL=stub (README, 'Run it')")
        base_url, token = settings.gateway_url, settings.token
    client = chatlas.ChatOpenAICompletions(
        base_url=base_url, api_key=token, model=settings.chat_model,
        kwargs={"http_client": httpx.AsyncClient(transport=StepLimit(), timeout=250.0)})  # 250 s, as main/model.py
    client.set_model_params(max_tokens=max_tokens_for(settings.chat_model), **sampling(settings.chat_model))  # as main/model.py
    return client

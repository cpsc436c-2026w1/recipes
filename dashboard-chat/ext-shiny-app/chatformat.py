"""One assistant message in the OpenAI chat-completions format, as an HTTP body: plain JSON, or the event stream
a client gets when it asks with "stream": true. Used by the stub gateway (stub_gateway.py) and by the step limit
of the page's client (client.py), which both answer in place of a model."""

from __future__ import annotations

import json


def completion(message: dict, model: str, stream: bool, usage: dict | None = None) -> tuple[str, bytes]:
    """(content type, body) for one assistant message: {"content": ...} or {"content": None, "tool_calls": [...]}."""
    usage = usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    finish = "tool_calls" if message.get("tool_calls") else "stop"
    if not stream:
        payload = {"id": "local", "object": "chat.completion", "created": 0, "model": model, "usage": usage,
                   "choices": [{"index": 0, "finish_reason": finish, "message": {"role": "assistant", **message}}]}
        return "application/json", json.dumps(payload).encode()
    delta = {"role": "assistant", "content": message.get("content")}
    if message.get("tool_calls"):
        delta["tool_calls"] = [{"index": i, **call} for i, call in enumerate(message["tool_calls"])]
    chunk = {"id": "local", "object": "chat.completion.chunk", "created": 0, "model": model}
    events = [{**chunk, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
              {**chunk, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]},
              {**chunk, "choices": [], "usage": usage}]
    return "text/event-stream", ("".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n").encode()

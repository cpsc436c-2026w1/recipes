"""A stand-in for an OpenAI-compatible endpoint, served through httpx.MockTransport: no network, no key.

`good=True` answers every feature the way the endpoint in output/ did. `good=False` is an endpoint that lists its
models and chats, and does nothing else right: no usage in the stream, max_tokens ignored, no tool call, prose where
a schema was asked for, no cache report, and embeddings that put the unrelated sentence closest.
"""

import json

import httpx

KEY = "test-key-not-real"
MODELS = ["fake-chat", "fake-embed"]


def _reply(content, *, finish="stop", tokens_out=2, cached=None, tool_calls=None):
    usage = {"prompt_tokens": 29, "completion_tokens": tokens_out, "total_tokens": 29 + tokens_out}
    if cached is not None:
        usage["prompt_tokens_details"] = {"cached_tokens": cached}
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {"choices": [{"index": 0, "message": message, "finish_reason": finish}], "usage": usage}


def handler(good=True, models=MODELS):
    def handle(request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization") != f"Bearer {KEY}":
            # Echo part of the key back, as some proxies do: render() must mask it.
            return httpx.Response(401, json={"error": {"message": f"bad key {request.headers.get('authorization')}"}})
        path = request.url.path
        if path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": m} for m in models]})
        body = json.loads(request.content)
        if body.get("model") not in models:
            return httpx.Response(404, json={"error": {"message": f"model {body.get('model')} not found; key {KEY}"}})
        if path.endswith("/embeddings"):
            vectors = [[1.0, 0.0], [0.9, 0.1], [0.0, 1.0]] if good else [[1.0, 0.0], [0.0, 1.0], [1.0, 0.1]]
            return httpx.Response(200, json={"data": [{"index": i, "embedding": v} for i, v in enumerate(vectors)],
                                             "usage": {"prompt_tokens": 21, "total_tokens": 21}})
        if body.get("stream"):
            chunks = ['data: {"choices": [{"delta": {"content": "ready"}}]}']
            if good and body.get("stream_options", {}).get("include_usage"):
                chunks.append('data: {"choices": [], "usage": {"prompt_tokens": 29, "completion_tokens": 2, "total_tokens": 31}}')
            chunks.append("data: [DONE]")
            return httpx.Response(200, text="\n\n".join(chunks) + "\n\n",
                                  headers={"content-type": "text/event-stream"})
        messages = body["messages"]
        if body.get("tools") and good:
            if messages[-1]["role"] == "tool":
                return httpx.Response(200, json=_reply("The trips table has 48,213 rows."))
            call = {"id": "call-1", "type": "function",
                    "function": {"name": "run_sql", "arguments": json.dumps({"query": "SELECT COUNT(*) FROM trips"})}}
            return httpx.Response(200, json=_reply(None, finish="tool_calls", tool_calls=[call]))
        if body.get("response_format"):
            text = '{"city": "Vancouver", "population": 660000}' if good else "Vancouver has 660 thousand residents."
            return httpx.Response(200, json=_reply(text))
        if body.get("max_tokens") == 5:
            return httpx.Response(200, json=_reply("1\n2\n3", finish="length", tokens_out=5) if good
                                  else _reply("1\n2\n3\n4\n5\n6\n7\n8", tokens_out=40))
        if len(messages[0]["content"]) > 2000:  # the long system prompt of ext-prompt-cache
            return httpx.Response(200, json=_reply("Section 7.", cached=3000 if good else None))
        return httpx.Response(200, json=_reply("ready"))

    return httpx.MockTransport(handle)

"""Stand-in for the upstream endpoint, for tests and local runs.

A request body may carry `x_fake` to steer it: `status` (answer with that
error), `chunks` (number of content chunks), `delay` (seconds between chunks or
before a buffered reply), `completion_tokens`, `no_usage` (never send usage),
`fail_after` (break the stream after that many chunks), `message` (the error message sent with
`status`), `raw_error` (an error body sent as is, instead of the OpenAI error shape).
"""

import asyncio
import json

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

received: list[dict] = []  # one entry per request: path, authorization, body


def prompt_tokens(messages: list) -> int:
    """Count like the gateway's reserve: bytes of content plus 16 per message."""
    return sum(len(str(m.get("content", "")).encode()) + 16 for m in messages)


def chunk_line(i: int) -> bytes:
    obj = {"id": "chatcmpl-fake", "object": "chat.completion.chunk",
           "choices": [{"index": 0, "delta": {"content": f"mot{i} é "}}]}
    return f"data: {json.dumps(obj)}\n\n".encode()


def usage_line(p: int, c: int) -> bytes:
    obj = {"id": "chatcmpl-fake", "object": "chat.completion.chunk", "choices": [],
           "usage": {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c}}
    return f"data: {json.dumps(obj)}\n\n".encode()


def render_stream(chunks: int, usage: tuple[int, int] | None) -> bytes:
    """The exact bytes this fake sends for a stream that ends normally."""
    out = b"".join(chunk_line(i) for i in range(chunks))
    if usage:
        out += usage_line(*usage)
    return out + b"data: [DONE]\n\n"


async def chat(request: Request):
    body = await request.json()
    received.append({"path": "/v1/chat/completions", "authorization": request.headers.get("authorization"),
                     "body": body})
    fake = body.get("x_fake", {})
    if fake.get("status"):
        headers = {"Retry-After": str(fake["retry_after"])} if fake.get("retry_after") else {}
        if "raw_error" in fake:  # a body that is not the OpenAI error shape
            return Response(fake["raw_error"], status_code=fake["status"], headers=headers)
        return JSONResponse({"error": {"message": fake.get("message", "internal upstream detail, do not leak")}},
                            status_code=fake["status"], headers=headers)
    chunks = fake.get("chunks", 3)
    delay = fake.get("delay", 0)
    p = prompt_tokens(body.get("messages", []))
    c = fake.get("completion_tokens", chunks)
    if not body.get("stream"):
        await asyncio.sleep(delay)
        reply = {"id": "chatcmpl-fake", "object": "chat.completion", "model": body["model"],
                 "choices": [{"index": 0, "message": {"role": "assistant", "content": "bonjour"},
                              "finish_reason": "stop"}]}
        if not fake.get("no_usage"):
            reply["usage"] = {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c}
        return JSONResponse(reply)
    include = (body.get("stream_options") or {}).get("include_usage") and not fake.get("no_usage")

    async def gen():
        for i in range(chunks):
            if fake.get("fail_after") == i:
                raise RuntimeError("fake upstream breaks the stream")
            yield chunk_line(i)
            await asyncio.sleep(delay)
        if include:
            yield usage_line(p, c)
        yield b"data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


async def embeddings(request: Request):
    body = await request.json()
    received.append({"path": "/v1/embeddings", "authorization": request.headers.get("authorization"),
                     "body": body})
    inputs = body["input"] if isinstance(body["input"], list) else [body["input"]]
    p = sum(len(x.encode()) for x in inputs)
    return JSONResponse({"object": "list", "model": body["model"],
                         "data": [{"object": "embedding", "index": i, "embedding": [0.1, 0.2, 0.3]}
                                  for i in range(len(inputs))],
                         "usage": {"prompt_tokens": p, "total_tokens": p}})


app = Starlette(routes=[
    Route("/v1/chat/completions", chat, methods=["POST"]),
    Route("/v1/embeddings", embeddings, methods=["POST"]),
])

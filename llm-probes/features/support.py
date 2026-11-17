"""The endpoint stand-in and the helpers the steps share. Nothing here opens a network connection.

The stand-in is tests/fake_endpoint.py (an OpenAI-compatible endpoint behind httpx.MockTransport) with three
additions a scenario can set: it keeps every request, it can answer one kind of request differently, and it stops
answering after REQUEST_LIMIT requests, so that a probe that loops fails and does not hang.
"""

from __future__ import annotations

import contextlib
import io
import json

import fake_endpoint
import httpx

URL = "https://endpoint.example/v1"     # a made-up address: the transport never leaves the process
KEY = fake_endpoint.KEY                 # a made-up key
REQUEST_LIMIT = 200


class Endpoint:
    def __init__(self):
        self.good = True                 # False: the endpoint that lists models and chats, and does nothing else right
        self.requests: list[httpx.Request] = []
        self.rules: list = []            # (matches(request, body), answer(request, body)) pairs, first match wins
        self.headers: dict[str, str] = {}
        self.models = list(fake_endpoint.MODELS)   # a scenario can list more chat models

    def when(self, matches, answer) -> None:
        self.rules.append((matches, answer))

    def transport(self) -> httpx.MockTransport:
        inner = fake_endpoint.handler(self.good, self.models).handler

        def handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            assert len(self.requests) <= REQUEST_LIMIT, "the stand-in received more requests than a run can need"
            body = json.loads(request.content) if request.content else {}
            for matches, answer in self.rules:
                if matches(request, body):
                    return answer(request, body)
            response = inner(request)
            response.headers.update(self.headers)
            return response

        return httpx.MockTransport(handle)

    def bodies(self) -> list[dict]:
        return [json.loads(r.content) for r in self.requests if r.content]


def chat_reply(content, *, finish="stop", tokens_out=2, reasoning=None, cached=None) -> httpx.Response:
    usage = {"prompt_tokens": 29, "completion_tokens": tokens_out, "total_tokens": 29 + tokens_out}
    if reasoning is not None:
        usage["completion_tokens_details"] = {"reasoning_tokens": reasoning}
    if cached is not None:
        usage["prompt_tokens_details"] = {"cached_tokens": cached}
    return httpx.Response(200, json={"choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                                                  "finish_reason": finish}], "usage": usage})


def run_captured(context, fn, *args, **kwargs):
    """Call fn; keep what it printed and its exit code (a SystemExit with a message is exit code 1, as in a shell)."""
    out, err = io.StringIO(), io.StringIO()
    context.exit_code = None
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            context.exit_code = fn(*args, **kwargs)
        except SystemExit as e:
            if e.code is None or isinstance(e.code, int):
                context.exit_code = e.code or 0
            else:
                context.exit_code = 1
                err.write(str(e.code) + "\n")
    context.printed += out.getvalue()
    context.printed_err += err.getvalue()


def table_rows(printed: str) -> list[dict]:
    """The rows of the printed table, as dicts with probe, model, works, seconds and note."""
    rows = []
    for line in printed.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if line.startswith("| ") and len(cells) == 5 and cells[0] not in ("probe", "---"):
            rows.append(dict(zip(("probe", "model", "works", "seconds", "note"), cells)))
    return rows

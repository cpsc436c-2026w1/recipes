"""Stand-ins and helpers the step files share. None of them opens a network connection.

The gateway stand-in is the recipe's own client (main/model.py) with its HTTP transport replaced by one that
answers inside the process (httpx.MockTransport): the address, the headers, the body and the time limit are the
ones the recipe built.
"""

from __future__ import annotations

import contextlib
import io
import json

import httpx

from main.model import GatewayModel
from main.router import ROUTER_SYSTEM

GATEWAY_URL = "https://gw.example/v1"   # a made-up address: the stand-in's transport never leaves the process
TOKEN = "tok-123-made-up"               # a made-up course token
REQUEST_LIMIT = 50                      # a stand-in answers at most this many requests, so a loop fails and does not hang


def over_stand_in(model: GatewayModel, handler) -> GatewayModel:
    """Replace the HTTP transport of a gateway client with `handler`; keep the address, headers and timeout."""
    built = model.http
    model.http = httpx.Client(base_url=built.base_url, headers=built.headers, timeout=built.timeout,
                              transport=httpx.MockTransport(handler))
    return model


def gateway_handler(requests: list, *, status: int = 200, router_reply: str = "hard", times_out: bool = False,
                    only_about: str = "", answer: str = "A reply from the stand-in.", only_model: str = "",
                    malformed: str = ""):
    """Answers a router call (its system prompt is the label definitions) with `router_reply` and any other call
    with a short reply. With `status` other than 200 every call is answered with that status and an error body; with `only_about`, only the calls whose user message holds that word."""

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert len(requests) <= REQUEST_LIMIT, "the stand-in received more requests than a scenario can need"
        if times_out:
            raise httpx.ReadTimeout("the stand-in did not answer", request=request)
        body = json.loads(request.content)
        if malformed and (not only_model or body["model"] == only_model):
            # status 200, and not a chat completion
            if malformed == "a page that is not JSON":
                return httpx.Response(200, text="<html>502 Bad Gateway</html>")
            if malformed == "an error object":
                return httpx.Response(200, json={"error": {"message": "upstream failed", "code": "upstream_error"}})
            if malformed == "an empty list of choices":
                return httpx.Response(200, json={"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 0}})
            raise AssertionError(malformed)
        if status != 200 and only_about in body["messages"][-1]["content"] and (
                not only_model or body["model"] == only_model):
            return httpx.Response(status, headers={"Retry-After": "60"},
                                  json={"error": {"message": "refused by the stand-in", "code": f"code-{status}"}})
        is_router = body["messages"][0]["content"] == ROUTER_SYSTEM
        return httpx.Response(200, json={
            "model": body["model"],
            "choices": [{"message": {"content": router_reply if is_router else answer}}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 100}})

    return handler


def bodies(requests: list) -> list[dict]:
    return [json.loads(r.content) for r in requests]


def run_captured(context, fn, *args, **kwargs):
    """Call fn; keep what it printed, its exit code (0 when it returned; a SystemExit with a message is 1, as in a
    shell) and, when it raised, the exception in context.error."""
    out, err = io.StringIO(), io.StringIO()
    result, context.exit_code, context.error = None, 0, None
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            result = fn(*args, **kwargs)
        except SystemExit as e:
            if e.code is None or isinstance(e.code, int):
                context.exit_code = e.code or 0
            else:
                context.exit_code = 1
                err.write(str(e.code) + "\n")
        except Exception as e:  # noqa: BLE001  a run that stops with an error: the scenario reads which
            context.exit_code, context.error = 1, e
            err.write(f"{type(e).__name__}: {e}\n")
    context.printed += out.getvalue()
    context.printed_err += err.getvalue()
    return result


def patch(context, obj, name: str, value) -> None:
    """Replace an attribute for this scenario only."""
    old = getattr(obj, name)
    setattr(obj, name, value)
    context.add_cleanup(setattr, obj, name, old)

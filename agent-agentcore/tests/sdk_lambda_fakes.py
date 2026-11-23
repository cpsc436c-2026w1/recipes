"""Offline stand-ins for ext-sdk-lambda: scripted models for both SDKs, a stand-in gateway that refuses, the data.

A script is a list of steps, one per model call (the last step repeats):
  ("sql", "SELECT ...")          the model calls run_sql
  ("output", {"reply", "value"}) the model gives its final TripAnswer
  ("prose", "text")              the model replies in plain text
A step may carry a third item, the seconds the call takes (asyncio.sleep, so a deadline can cancel it).
The two models play the same script: Strands' as a stream of events, Pydantic AI's as a FunctionModel.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXT = ROOT / "ext-sdk-lambda"
if str(EXT) not in sys.path:
    sys.path.append(str(EXT))          # appended: the recipe's own modules (clean_up, main) keep their names
os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")


def load(name: str, alias: str | None = None):
    """A module of ext-sdk-lambda by file, under its own name or `alias` (clean_up.py is also a root module)."""
    alias = alias or name
    if alias in sys.modules:
        return sys.modules[alias]
    spec = importlib.util.spec_from_file_location(alias, EXT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


def ext_env(env=os.environ) -> None:
    """The lines of ext-sdk-lambda/.env.example, then test values: no gateway, no token, no real account."""
    for line in (EXT / ".env.example").read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            name, _, value = line.partition("=")
            env[name] = value
    env.update({"PREFIX": "test", "COURSE_GATEWAY_URL": "http://127.0.0.1:9/v1", "KB_MCP_TOKEN": "test-token",
                "AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_SESSION_TOKEN": "testing",
                "AWS_DEFAULT_REGION": "ca-central-1", "AWS_EC2_METADATA_DISABLED": "true"})
    env.pop("AWS_PROFILE", None)


def trips_file(folder: Path) -> Path:
    """The synthetic SIM01 stand-in (make_trips.py --synthetic), built once into `folder`."""
    path = folder / "trips.parquet"
    if not path.exists():
        make = load("make_trips")
        make.synthetic(path)
        assert make.check(path) == []
    return path


def _step(script: list, n: int):
    step = script[min(n, len(script)) - 1]
    return step[0], step[1], (step[2] if len(step) > 2 else 0.0)


class ScriptedStrands:
    """A Strands model that plays a script. `calls` counts the calls it started; `cancelled` those cut off."""

    def __new__(cls, script, scale: float = 1.0, on_call=None):
        from strands.models import Model

        class _Model(Model):
            def __init__(self):
                self.script, self.scale, self.calls, self.cancelled, self.started = list(script), scale, 0, 0, []

            def update_config(self, **kw):
                pass

            def get_config(self):
                return {"model_id": "scripted"}

            async def structured_output(self, *a, **kw):
                raise NotImplementedError

            async def stream(self, messages, tool_specs=None, system_prompt=None, **kw):
                self.calls += 1
                if on_call:
                    on_call(self.calls)
                kind, content, takes = _step(self.script, self.calls)
                self.started.append(asyncio.get_running_loop().time())
                try:
                    await asyncio.sleep(takes * self.scale)
                except asyncio.CancelledError:
                    self.cancelled += 1
                    raise
                yield {"messageStart": {"role": "assistant"}}
                if kind in ("sql", "output"):
                    name, args = ("run_sql", {"sql": content}) if kind == "sql" else ("TripAnswer", content)
                    yield {"contentBlockStart": {"start": {"toolUse": {"name": name, "toolUseId": f"s-{self.calls}"}}}}
                    yield {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(args)}}}}
                    yield {"contentBlockStop": {}}
                    yield {"messageStop": {"stopReason": "tool_use"}}
                else:
                    yield {"contentBlockStart": {"start": {}}}
                    yield {"contentBlockDelta": {"delta": {"text": content}}}
                    yield {"contentBlockStop": {}}
                    yield {"messageStop": {"stopReason": "end_turn"}}
                yield {"metadata": {"usage": {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0},
                                    "metrics": {"latencyMs": 0}}}

        return _Model()


class ScriptedPydantic:
    """Pydantic AI's FunctionModel playing a script: the same replies as ScriptedStrands."""

    def __new__(cls, script, scale: float = 1.0, on_call=None):
        from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
        from pydantic_ai.models.function import FunctionModel

        state = {"calls": 0, "cancelled": 0, "started": []}

        async def reply(messages, info):
            state["calls"] += 1
            if on_call:
                on_call(state["calls"])
            state["started"].append(asyncio.get_running_loop().time())
            kind, content, takes = _step(script, state["calls"])
            try:
                await asyncio.sleep(takes * scale)
            except asyncio.CancelledError:
                state["cancelled"] += 1
                raise
            if kind == "sql":
                return ModelResponse(parts=[ToolCallPart("run_sql", {"sql": content}, tool_call_id=f"p-{state['calls']}")])
            if kind == "output":
                return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, content,
                                                         tool_call_id=f"p-{state['calls']}")])
            return ModelResponse(parts=[TextPart(content)])

        model = FunctionModel(reply)
        model.state = state
        return model


def scripted(sdk: str, script, scale: float = 1.0, on_call=None):
    """on_call(n): called as model call n starts (a test moves its clock there)."""
    return ScriptedStrands(script, scale, on_call) if sdk == "strands" else ScriptedPydantic(script, scale, on_call)


def calls_of(model) -> int:
    return model.calls if hasattr(model, "calls") else model.state["calls"]


def started_of(model) -> list[float]:
    """The loop time at which each model call started."""
    return model.started if hasattr(model, "started") else model.state["started"]


def cancelled_of(model) -> int:
    return model.cancelled if hasattr(model, "cancelled") else model.state["cancelled"]


class RefusingGateway:
    """A stand-in for the course LLM gateway that refuses every /chat/completions with one status and code, the way
    the gateway does: {"error": {"message", "type", "code"}} and Retry-After when given. Keeps every request."""

    TYPES = {"team_budget_exhausted": "insufficient_quota", "rate_limit_exceeded": "rate_limit_exceeded",
             "upstream_rate_limited": "rate_limit_exceeded", "invalid_api_key": "invalid_request_error",
             "key_revoked": "invalid_request_error", "key_expired": "invalid_request_error",
             "model_not_allowed": "invalid_request_error", "upstream_error": "upstream_error"}

    def __init__(self):
        self.requests: list[str] = []
        self.answer = (429, "team_budget_exhausted", None)
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                outer.requests.append(self.path)
                status, code, retry_after = outer.answer
                body = json.dumps({"error": {"message": f"refused by the stand-in: {code}",
                                             "type": outer.TYPES.get(code, "error"), "code": code}}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                if retry_after:
                    self.send_header("Retry-After", str(retry_after))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

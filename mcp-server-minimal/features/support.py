"""Helpers the step files share: the recipe's server on a free localhost port, tool calls with the recipe's client
code, and what a run printed. Nothing here stands in for the server, the engine or the client."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import socket
import threading
import time

import uvicorn

from main.client import connect

TOKEN = "scenario-token-not-a-secret-0123456789"       # made up; at least 16 characters
SECRET = "scenario-approval-secret-not-real-0000"      # made up; at least 16 characters


class LogLines(logging.Handler):
    """Keeps every line the recipe's logger wrote in a scenario: the text, and the JSON when the line is JSON."""

    def __init__(self):
        super().__init__(level=logging.INFO)
        self.texts: list[str] = []

    def emit(self, record):
        self.texts.append(record.getMessage())

    def json(self) -> list[dict]:
        return [json.loads(t) for t in self.texts if t.startswith("{")]


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def serve(context, app) -> str:
    """Start an ASGI app on a free port of 127.0.0.1; it is stopped when the scenario ends."""
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started:
        assert time.time() < deadline, "the server did not start within 10 seconds"
        time.sleep(0.02)
    context.servers.append((server, thread))
    return f"http://127.0.0.1:{port}/mcp"


def call_tool(url: str, token: str, tool: str, args: dict) -> dict:
    """One tool call over HTTP with the recipe's client code. Returns is_error and the result (JSON when the tool
    answered, the error text when the result is marked as an error)."""

    async def go():
        async with connect(url, token) as client:
            r = await client.call_tool(tool, args)
            text = r.content[0].text
            return {"is_error": r.is_error, "result": text if r.is_error else json.loads(text)}

    return asyncio.run(asyncio.wait_for(go(), timeout=30))


def list_tools(url: str, token: str) -> dict:
    async def go():
        async with connect(url, token) as client:
            return {t.name: t.annotations for t in (await client.list_tools()).tools}

    return asyncio.run(asyncio.wait_for(go(), timeout=30))


def run_captured(context, fn, *args, **kwargs):
    """Call fn; keep what it printed and its exit code (0 when it returned None or 0; a SystemExit with a message
    is exit code 1, as in a shell)."""
    out, err = io.StringIO(), io.StringIO()
    result, context.exit_code = None, 0
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            result = fn(*args, **kwargs)
            if isinstance(result, int):
                context.exit_code = result
        except SystemExit as e:
            if e.code is None or isinstance(e.code, int):
                context.exit_code = e.code or 0
            else:
                context.exit_code = 1
                err.write(str(e.code) + "\n")
    context.printed += out.getvalue()
    context.printed_err += err.getvalue()
    return result


def patch(context, obj, name: str, value) -> None:
    """Replace an attribute for this scenario only."""
    old = getattr(obj, name)
    setattr(obj, name, value)
    context.add_cleanup(setattr, obj, name, old)

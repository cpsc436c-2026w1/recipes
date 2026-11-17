"""Shared fixtures: sample data in a temporary folder, and a real server on a free localhost port.

The tests talk to the server over HTTP with the same client code as main/client.py, so a passing test means the
transport, the token check and the tools work together, not only the functions one by one.
"""

from __future__ import annotations

import socket
import threading
import time

import pytest
import uvicorn

from main.config import Settings
from main.data import ensure_data
from main.engine import ReadOnlyEngine

TOKEN = "test-token-not-a-secret-0123456789"


@pytest.fixture(scope="session")
def tables(tmp_path_factory):
    return ensure_data(tmp_path_factory.mktemp("data"))


@pytest.fixture
def engine(tables):
    e = ReadOnlyEngine(tables, max_rows=50)
    yield e
    e.close()


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def serve(app) -> tuple[str, uvicorn.Server, threading.Thread]:
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started:
        if time.time() > deadline:
            raise RuntimeError("test server did not start")
        time.sleep(0.02)
    return f"http://127.0.0.1:{port}/mcp", server, thread


@pytest.fixture
def settings(tmp_path):
    return Settings(token=TOKEN, data_dir=tmp_path)


@pytest.fixture
def running(settings, engine):
    """URL of a running main server."""
    from main.server import build_app, build_server

    url, server, thread = serve(build_app(settings, build_server(engine)))
    yield url
    server.should_exit = True
    thread.join(timeout=5)

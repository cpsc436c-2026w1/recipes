"""A client that does what an assistant's MCP client does: list the tools, then call them.

Run (server running in another terminal, same MCP_SERVER_TOKEN):  uv run python -m main.client

It calls get_schema, one SELECT, and one DELETE to show the refusal. An assistant (Claude Code, an IDE agent, your
own tool-calling loop from L15) does the same calls; the model chooses which tool and which SQL.
"""

from __future__ import annotations

import argparse
import asyncio
import json

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from main.config import Settings

EXAMPLE_QUERIES = [
    "SELECT s.city, count(*) AS orders, round(sum(o.amount_cad), 2) AS sales_cad "
    "FROM orders o JOIN stores s USING (store_id) GROUP BY s.city ORDER BY sales_cad DESC",
    "DELETE FROM orders",
]


def connect(url: str, token: str) -> Client:
    # The token travels in the Authorization header of every request; it is read from the environment only.
    http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=30)
    return Client(streamable_http_client(url, http_client=http))


async def run(url: str, token: str, queries: list[str]) -> list[dict]:
    """Returns one dict per step, so tests can check what the printout shows."""
    steps: list[dict] = []
    async with connect(url, token) as client:
        listed = await client.list_tools()
        steps.append({"step": "list_tools",
                      "tools": [{"name": t.name, "read_only": bool(t.annotations and t.annotations.read_only_hint)}
                                for t in listed.tools]})
        schema = await client.call_tool("get_schema", {})
        steps.append({"step": "get_schema", "is_error": schema.is_error,
                      "tables": [t["table"] for t in json.loads(schema.content[0].text)]})
        for sql in queries:
            r = await client.call_tool("query", {"sql": sql})
            text = r.content[0].text
            steps.append({"step": "query", "sql": sql, "is_error": r.is_error,
                          "result": text if r.is_error else json.loads(text)})
    return steps


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--url", default=None, help="default http://MCP_HOST:MCP_PORT/mcp")
    p.add_argument("--sql", action="append", help="a query to send; repeat for several")
    args = p.parse_args(argv)
    settings = Settings()
    if not settings.token:
        raise SystemExit("set MCP_SERVER_TOKEN to the server's token first (README, 'Run it')")
    url = args.url or settings.url
    try:
        steps = asyncio.run(run(url, settings.token, args.sql or EXAMPLE_QUERIES))
    except Exception as e:  # the SDK wraps HTTP errors in exception groups; say what went wrong in one line
        raise SystemExit(plain_error(e, url, settings.token)) from None
    for step in steps:
        print(json.dumps(step))


def _leaves(e: BaseException):
    if isinstance(e, BaseExceptionGroup):
        for sub in e.exceptions:
            yield from _leaves(sub)
    else:
        yield e
        if e.__cause__ is not None:
            yield from _leaves(e.__cause__)


def _status(url: str, token: str) -> int | None:
    """One plain POST to ask the endpoint for its HTTP status: the SDK's error does not carry it."""
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url, data=b"{}", method="POST", headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as h:
        return h.code
    except OSError:
        return None


def plain_error(e: Exception, url: str, token: str = "") -> str:
    text = " ".join(f"{type(x).__name__}: {x}" for x in _leaves(e))
    if "401" in text or (token and _status(url, token) == 401):
        return f"401 Unauthorized from {url}: MCP_SERVER_TOKEN here is not the server's token"
    if "Connect" in text:
        return f"cannot reach {url}: is the server running in the other terminal (uv run python -m main.server)?"
    return f"MCP call failed: {text}"


if __name__ == "__main__":
    main()

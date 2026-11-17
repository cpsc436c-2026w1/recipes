"""Validation of the main scenario over HTTP: a client lists the tools and gets a query result; a non-SELECT is
refused; a request without the token does not reach the tools."""

from __future__ import annotations

import asyncio

import pytest

from main.client import run
from tests.conftest import TOKEN


def test_client_lists_two_read_only_tools_and_gets_rows(running):
    steps = asyncio.run(run(running, TOKEN, ["SELECT count(*) AS n FROM orders"]))
    listed, schema, query = steps
    assert {t["name"] for t in listed["tools"]} == {"get_schema", "query"}
    assert all(t["read_only"] for t in listed["tools"])
    assert sorted(schema["tables"]) == ["orders", "stores"]
    assert not query["is_error"]
    assert query["result"]["rows"] == [[200]]


def test_non_select_is_refused_and_changes_nothing(running):
    steps = asyncio.run(run(running, TOKEN, ["DELETE FROM orders", "SELECT count(*) FROM orders"]))
    refused, after = steps[2], steps[3]
    assert refused["is_error"] and "only SELECT" in refused["result"]
    assert after["result"]["rows"] == [[200]]


def test_row_cap_is_reported(running):
    steps = asyncio.run(run(running, TOKEN, ["SELECT * FROM orders"]))
    result = steps[2]["result"]
    assert len(result["rows"]) == 50 and result["truncated"] is True


@pytest.mark.parametrize("auth", [None, "Bearer wrong-token-of-enough-length-0000", f"Basic {TOKEN}"])
def test_without_the_right_token_the_server_answers_401(running, auth):
    import httpx2

    headers = {"Accept": "application/json, text/event-stream"}
    if auth:
        headers["Authorization"] = auth
    r = httpx2.post(running, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=headers)
    assert r.status_code == 401


def test_client_with_a_wrong_token_does_not_get_the_tools(running):
    with pytest.raises(BaseException):
        asyncio.run(run(running, "wrong-token-of-enough-length-0000", []))


def test_client_says_401_in_one_line_for_a_wrong_token(running):
    from main.client import plain_error

    wrong = "wrong-token-of-enough-length-0000"
    with pytest.raises(BaseException) as e:
        asyncio.run(run(running, wrong, []))
    assert plain_error(e.value, running, wrong).startswith("401 Unauthorized")


def test_server_refuses_to_start_without_a_token(engine):
    from main.config import Settings
    from main.server import build_app, build_server

    with pytest.raises(SystemExit):
        build_app(Settings(token=""), build_server(engine))

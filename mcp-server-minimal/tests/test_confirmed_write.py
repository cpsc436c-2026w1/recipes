"""Validation of ext-confirmed-write: the write is refused without a person's code and applies once with it."""

from __future__ import annotations

import asyncio

import pytest

from confirm import Refund, check_code, make_code
from store import RefundStore
from tests.conftest import TOKEN, serve
from walk import walk
from write_server import build_app, build_write_server

SECRET = "approval-secret-for-tests-only-0000"


@pytest.fixture
def write_server(settings, engine, tmp_path):
    store = RefundStore(tmp_path / "refunds.duckdb")
    url, server, thread = serve(build_app(settings, build_write_server(engine, store, SECRET)))
    yield url, store
    server.should_exit = True
    thread.join(timeout=5)
    store.close()


def test_refused_without_confirmation_and_applied_once_with_it(write_server):
    url, store = write_server
    steps = {s["step"]: s for s in asyncio.run(walk(url, TOKEN, SECRET, "ana"))}

    first = steps["no code"]
    assert not first["is_error"] and first["result"]["status"] == "needs_confirmation"
    assert "approve.py" in first["result"]["ask_the_person_to_run"]
    assert steps["made-up code"]["is_error"]
    assert steps["code for a larger amount"]["is_error"]          # a code fits one exact change

    applied = steps["with code"]["result"]
    assert applied["status"] == "applied" and applied["approved_by"] == "ana"
    again = steps["same call again"]["result"]
    assert again["status"] == "already_applied" and again["changed"] is False
    assert store.count() == 1                                      # four calls, one row


def test_request_id_reused_for_another_change_is_refused(tmp_path):
    store = RefundStore(tmp_path / "r.duckdb")
    assert store.apply(Refund("req-0001", 1042, 10.0, "late"), "ana")["status"] == "applied"
    assert store.apply(Refund("req-0001", 1042, 99.0, "late"), "ana")["status"] == "conflict"
    assert store.count() == 1


def test_invalid_refunds_are_refused_before_any_approval(write_server):
    url, store = write_server
    from main.client import connect

    async def go():
        async with connect(url, TOKEN) as c:
            missing = await c.call_tool("record_refund", {"request_id": "req-x1", "order_id": 9, "amount_cad": 1,
                                                          "reason": "x"})
            too_much = await c.call_tool("record_refund", {"request_id": "req-x2", "order_id": 1042,
                                                           "amount_cad": 10_000, "reason": "x"})
            listed = await c.list_tools()
            return missing, too_much, listed

    missing, too_much, listed = asyncio.run(go())
    assert missing.is_error and "does not exist" in missing.content[0].text
    assert too_much.is_error and "at most" in too_much.content[0].text
    tools = {t.name: t.annotations for t in listed.tools}
    assert set(tools) == {"get_schema", "query", "record_refund"}
    assert tools["record_refund"].read_only_hint is False and tools["query"].read_only_hint is True
    assert store.count() == 0


def test_code_binds_approver_and_change():
    r = Refund("req-0002", 1001, 5.0, "damaged")
    code = make_code(SECRET, "ana", r)
    assert check_code(SECRET, code, r) == "ana"
    assert check_code(SECRET, code.replace("ana.", "bob."), r) is None
    assert check_code(SECRET, code, Refund("req-0002", 1001, 5.01, "damaged")) is None
    assert check_code("another-secret-0000000000", code, r) is None
    assert check_code(SECRET, "ana.9999999999.caf\u00e9456789abcdef", r) is None   # non-ASCII text is refused, not a crash
    assert check_code(SECRET, "ana.\u0661\u0662.0123456789abcdef", r) is None      # digits that are not ASCII
    assert check_code(SECRET, make_code(SECRET, "ana", r, expires_at=1), r) is None  # a code past its time
    assert check_code(SECRET, "", r) is None and check_code(SECRET, "ana", r) is None


def test_approve_script_prints_the_same_code(monkeypatch, capsys):
    import approve

    monkeypatch.setenv("APPROVAL_SECRET", SECRET)
    rc = approve.main(["--request-id", "req-0003", "--order-id", "1001", "--amount", "5", "--reason", "damaged",
                       "--approver", "ana", "--yes"])
    assert rc == 0
    assert make_code(SECRET, "ana", Refund("req-0003", 1001, 5.0, "damaged")) in capsys.readouterr().out

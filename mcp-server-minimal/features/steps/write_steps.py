"""Steps for ext-confirmed-write: the write server, the person's approval, walk.py."""

from __future__ import annotations

import builtins
import os
import re
import time

import approve
import walk as walk_module
import write_server as write_module
from behave import given, then, when
from confirm import APPROVAL_TTL_S, Refund, make_code
from store import RefundStore
from support import SECRET, TOKEN, call_tool, patch, run_captured, serve

from main.config import Settings
from main.engine import ReadOnlyEngine


def _text(value: str) -> str:
    """"<65 x>" in a table cell is 65 times the letter x."""
    m = re.fullmatch(r"<(\d+) x>", value)
    return "x" * int(m.group(1)) if m else value


def _args(request, order, amount, reason) -> dict:
    return {"request_id": _text(request), "order_id": order, "amount_cad": float(amount), "reason": _text(reason)}


@given("the write server is running with the sample tables and an empty refunds database")
def write_server(context):
    context.engine = ReadOnlyEngine(context.tables, max_rows=Settings().max_rows)
    context.store = RefundStore(context.tmp / "refunds.duckdb")
    settings = Settings(token=TOKEN, data_dir=context.data_dir)
    mcp = write_module.build_write_server(context.engine, context.store, SECRET)
    context.url = serve(context, write_module.build_app(settings, mcp))


@given('a person approved request "{request}", order {order:d}, {amount} CAD, reason "{reason}" as "{approver}"')
def person_approved(context, request, order, amount, reason, approver):
    context.code = make_code(SECRET, approver, Refund(request, order, round(float(amount), 2), reason))


@given('a person approved request "{request}", order {order:d}, {amount} CAD, reason "{reason}" as "{approver}", {minutes:d} minutes ago')
def person_approved_earlier(context, request, order, amount, reason, approver, minutes):
    made_at = int(time.time()) - minutes * 60                     # the code is made as approve.py makes it
    context.code = make_code(SECRET, approver, Refund(request, order, round(float(amount), 2), reason),
                             expires_at=made_at + APPROVAL_TTL_S)


@when('the model calls record_refund for request "{request}", order {order:d}, {amount} CAD, reason "{reason}" with the person\'s code with a later expiry')
def call_with_later_expiry(context, request, order, amount, reason):
    approver, expiry, mac = context.code.split(".")
    context.result = call_tool(context.url, TOKEN, "record_refund", {
        **_args(request, order, amount, reason), "confirmation": f"{approver}.{int(expiry) + 3600}.{mac}"})


@then("the printed code expires {minutes:d} minutes from now")
def code_expiry(context, minutes):
    code = re.search(r"Confirmation code: (\S+)", context.printed).group(1)
    left = int(code.split(".")[1]) - time.time()
    assert minutes * 60 - 10 <= left <= minutes * 60, left


@given("APPROVAL_SECRET is the server's approval secret")
def secret_set(context):
    os.environ["APPROVAL_SECRET"] = SECRET


@given("APPROVAL_SECRET is not set")
def secret_not_set(context):
    os.environ.pop("APPROVAL_SECRET", None)


@given('APPROVAL_SECRET is "{secret}"')
def secret_is(context, secret):
    os.environ["APPROVAL_SECRET"] = secret


@given('the model calls record_refund for request "{request}", order {order:d}, {amount} CAD, reason "{reason}" with the person\'s code')
@when('the model calls record_refund for request "{request}", order {order:d}, {amount} CAD, reason "{reason}" with the person\'s code')
def call_with_code(context, request, order, amount, reason):
    context.result = call_tool(context.url, TOKEN, "record_refund",
                               {**_args(request, order, amount, reason), "confirmation": context.code})


@when('the model calls record_refund for request "{request}", order {order:d}, {amount} CAD, reason "{reason}" with the person\'s code under the name "{name}"')
def call_with_code_renamed(context, request, order, amount, reason, name):
    code = name + "." + context.code.split(".", 1)[1]
    context.result = call_tool(context.url, TOKEN, "record_refund",
                               {**_args(request, order, amount, reason), "confirmation": code})


@when('the model calls record_refund for request "{request}", order {order:d}, {amount} CAD, reason "{reason}" with the code "{code}"')
def call_with_given_code(context, request, order, amount, reason, code):
    context.result = call_tool(context.url, TOKEN, "record_refund",
                               {**_args(request, order, amount, reason), "confirmation": code})


@when('the model calls record_refund for request "{request}", order {order:d}, {amount} CAD, reason "{reason}"')
def call_without_code(context, request, order, amount, reason):
    context.result = call_tool(context.url, TOKEN, "record_refund", _args(request, order, amount, reason))


@when('approve.py runs for request "{request}", order {order:d}, {amount} CAD, reason "{reason}" as "{approver}" and the person answers "{answer}"')
def approve_runs(context, request, order, amount, reason, approver, answer):
    patch(context, builtins, "input", lambda prompt="": answer)
    run_captured(context, approve.main, ["--request-id", request, "--order-id", str(order), "--amount", amount,
                                         "--reason", reason, "--approver", approver])


@when("ext-confirmed-write/write_server.py is started")
def start_write_server(context):
    from server_steps import _start

    _start(context, write_module.main)


@when("ext-confirmed-write/walk.py runs against the server")
def walk_runs(context):
    os.environ.update({"MCP_SERVER_TOKEN": TOKEN, "APPROVAL_SECRET": SECRET,
                       "MCP_PORT": context.url.rsplit(":", 1)[1].split("/")[0]})
    run_captured(context, walk_module.main, ["--approver", "ana"])


@then('the result has status "{status}" and changed {changed}')
def status_and_changed(context, status, changed):
    r = context.result
    assert r["is_error"] is False and (r["result"]["status"], r["result"]["changed"]) == (status, changed == "true"), r


@then('the result\'s change is request "{request}", order {order:d}, amount "{amount}", reason "{reason}"')
def result_change(context, request, order, amount, reason):
    assert context.result["result"]["change"] == {"request_id": request, "order_id": order, "amount_cad": amount,
                                                  "reason": reason}, context.result


@then('the result names the command "{command}"')
def result_command(context, command):
    assert context.result["result"]["ask_the_person_to_run"] == command, context.result["result"]


@then("the refunds table has {n:d} row")
@then("the refunds table has {n:d} rows")
def refunds_rows(context, n):
    assert context.store.count() == n, context.store.count()


@then('the row of "{request}" has order {order:d}, amount {amount}, approver "{approver}" and a UTC time')
def refund_row(context, request, order, amount, approver):
    row = context.store.con.execute(
        "SELECT order_id, amount_cad, approved_by, applied_at FROM refunds WHERE request_id = ?", [request]).fetchone()
    assert (row[0], f"{float(row[1]):.2f}", row[2]) == (order, amount, approver), row
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00", row[3]), row[3]


@then("no confirmation code is printed")
def no_code(context):
    assert "Confirmation code" not in context.printed and not re.search(r"\b[a-z][a-z0-9-]+\.\d+\.[0-9a-f]{16}\b",
                                                                        context.printed)


@then('the printed code is accepted by record_refund for request "{request}", order {order:d}, {amount} CAD, reason "{reason}"')
def printed_code_accepted(context, request, order, amount, reason):
    code = re.search(r"Confirmation code: (\S+)", context.printed).group(1)
    result = call_tool(context.url, TOKEN, "record_refund",
                       {**_args(request, order, amount, reason), "confirmation": code})
    assert result["is_error"] is False and result["result"]["status"] == "applied", result


@then("the approval secret is in no log line and in no printed line")
def secret_nowhere(context):
    everything = "\n".join(context.log_lines.texts) + context.printed + context.printed_err
    assert context.log_lines.texts and context.printed and SECRET not in everything

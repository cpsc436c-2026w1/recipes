"""How the steps run the recipe and what stands in for AWS. Nothing here opens a network connection.

Calls sits where boto3 hands a call to the network (botocore's BaseClient._make_api_call). It writes down every
call (operation, parameters) and, for the calls a scenario names, answers in place of DynamoDB: a refusal with an
error code of the API reference, a table in the DELETING state, a reply that never comes. Every other call goes on
to moto.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import runpy
import sys
import threading
from pathlib import Path

import botocore.client
from botocore.exceptions import ClientError, ReadTimeoutError

ROOT = Path(__file__).resolve().parent.parent
PREFIX = "recipe-ana"
REPEAT_LIMIT = 5


class Calls:
    def __init__(self):
        self.made: list[tuple[str, dict]] = []      # (operation, parameters), in the order the calls started
        self.rules: list[dict] = []
        self.regions: list[str] = []                # the Region of the client that made each call
        self._lock = threading.Lock()
        self._one_at_a_time = threading.Lock()
        self.refused = 0                            # conditional writes that were refused

    def start(self):
        self._real = botocore.client.BaseClient._make_api_call
        calls = self

        def make_api_call(client, operation, params):
            with calls._lock:
                calls.made.append((operation, dict(params)))
                calls.regions.append(client.meta.region_name)
                # A waiter that will never be satisfied asks the same thing again and again, for minutes. A scenario
                # must fail, not hang: the 5th identical look in a row ends it.
                if len(calls.made) >= REPEAT_LIMIT and all(c == calls.made[-1] for c in calls.made[-REPEAT_LIMIT:]):
                    raise RuntimeError(f"{REPEAT_LIMIT} identical {operation} calls in a row: the recipe is waiting "
                                       f"for something that will not happen in this scenario")
                rule = next((r for r in calls.rules if r["operation"] == operation and r["left"] != 0
                             and r["when"](params)), None)
                if rule:
                    rule["left"] -= 1
            if rule is None:
                # One call at a time, as main/local.py does for --target local: DynamoDB checks a condition and
                # writes in one step, moto does not. Without this, a run with --target aws inside moto told both
                # drivers yes in safe mode about once in five runs.
                with calls._one_at_a_time:
                    try:
                        return calls._real(client, operation, params)
                    except ClientError as err:
                        if err.response["Error"]["Code"] == "ConditionalCheckFailedException":
                            calls.refused += 1          # moto (as DynamoDB) refused a conditional write
                        raise
            if rule["kind"] == "refuse":
                raise client.exceptions.from_code(rule["code"])(
                    {"Error": {"Code": rule["code"], "Message": "put in by the scenario"}}, operation)
            if rule["kind"] == "no_reply":
                raise ReadTimeoutError(endpoint_url="https://dynamodb.ca-central-1.amazonaws.com")
            if rule["kind"] == "deleting":
                # the table is on its way out: this look still sees it, in the DELETING state, and then it is gone
                with contextlib.suppress(ClientError):
                    calls._real(client, "DeleteTable", {"TableName": params["TableName"]})
                return {"Table": {"TableName": params["TableName"], "TableStatus": "DELETING"}}
            return rule["answer"]

        botocore.client.BaseClient._make_api_call = make_api_call

    def stop(self):
        botocore.client.BaseClient._make_api_call = self._real

    def _add(self, operation, kind, times, when=None, **more):
        self.rules.append({"operation": operation, "kind": kind, "left": times, "when": when or (lambda p: True), **more})

    def refuse(self, operation, code, times=-1, when=None):
        """The next `times` calls (-1: every call) are refused with this error code."""
        self._add(operation, "refuse", times, when, code=code)

    def answer(self, operation, answer, times=-1, when=None):
        self._add(operation, "answer", times, when, answer=answer)

    def deleting(self, table: str):
        """The next DescribeTable of this table answers DELETING (DynamoDB API reference, TableStatus), and the
        table is gone by the look after it."""
        self._add("DescribeTable", "deleting", 1, lambda p: p.get("TableName") == table)

    def no_reply(self, operation, times=-1):
        """The call never gets a reply: botocore's ReadTimeoutError, as after read_timeout."""
        self._add(operation, "no_reply", times)

    def of(self, operation: str) -> list[dict]:
        return [p for op, p in self.made if op == operation]

    def names(self) -> list[str]:
        return [op for op, _ in self.made]


def load(name: str, rel: str):
    """A module by its path: main/run.py and ext-idempotent-retry/run.py are both called run."""
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(context, fn, *args, **kwargs):
    """Call fn; keep what it printed, its exit code (0 when it returned) and an error it raised."""
    out = io.StringIO()
    result, context.exit_code, context.error = None, 0, None
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        try:
            result = fn(*args, **kwargs)
        except SystemExit as e:
            context.exit_code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        except Exception as e:            # noqa: BLE001  the scenario states which error it expects
            context.exit_code, context.error = 1, e
    context.printed += out.getvalue()
    return result


def run_main(context, rel: str, args: list[str], prefix: str | None = PREFIX):
    if prefix is None:
        os.environ.pop("PREFIX", None)
    else:
        os.environ["PREFIX"] = prefix
    module = load("recipe_run_" + rel.replace("/", "_").replace("-", "_").removesuffix(".py"), rel)
    return run(context, module.main, args)


def run_clean_up(context, prefix: str | None = PREFIX):
    """clean_up.py as the command line runs it."""
    if prefix is None:
        os.environ.pop("PREFIX", None)
    else:
        os.environ["PREFIX"] = prefix
    sys.modules.pop("clean_up", None)
    saved, sys.argv = sys.argv, [str(ROOT / "clean_up.py"), "--yes"]    # a bare run only lists (the contract)
    try:
        return run(context, runpy.run_path, str(ROOT / "clean_up.py"), run_name="__main__")
    finally:
        sys.argv = saved


def ddb():
    import boto3

    return boto3.client("dynamodb", region_name="ca-central-1")


def tables() -> list[str]:
    return sorted(ddb().list_tables()["TableNames"])


def report_rows(printed: str) -> dict[str, dict]:
    """The table main/run.py prints: mode -> its four counts."""
    rows = {}
    for line in printed.splitlines():
        parts = line.split()
        if len(parts) == 5 and parts[0] in ("unsafe", "safe"):
            rows[parts[0]] = dict(zip(("rounds", "one_winner", "both_told_yes", "winner_is_stored"), map(int, parts[1:])))
    return rows

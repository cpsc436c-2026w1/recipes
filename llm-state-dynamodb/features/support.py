"""Helpers for the steps: a clock the scenarios move, a record of every AWS call, captured runs of the scripts."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import socket
import sys
from pathlib import Path

import botocore.client
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parent.parent
CALL_LIMIT = 200  # a mutated loop fails at this many calls of one operation, instead of hanging


class Clock:
    """Seconds since 1970, moved by the scenarios; starts at 2027-01-15 08:00 UTC."""

    def __init__(self, t: float = 1_800_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class Calls:
    """Sits where boto3 hands a call to the network (and to moto behind it).

    It writes down every call with its parameters. For an operation a scenario names, it answers in place of AWS:
    an error with an error code from the API reference, or a reply. Everything else goes on to moto."""

    def __init__(self):
        self.log: list[tuple[str, dict]] = []
        self.rules: list[dict] = []

    def answer(self, operation: str, *, when=lambda params: True, error: str = "", reply: dict | None = None,
               times: int | None = None, extra: dict | None = None, params=None) -> None:
        """`params`: a function that changes the call's parameters before it goes on to moto (no reply)."""
        self.rules.append({"op": operation, "when": when, "error": error, "reply": reply, "times": times,
                           "extra": extra or {}, "params": params})

    def of(self, operation: str) -> list[dict]:
        return [params for op, params in self.log if op == operation]

    def install(self, context) -> None:
        real = botocore.client.BaseClient._make_api_call
        calls = self

        def make_api_call(client, operation, params):
            calls.log.append((operation, dict(params)))
            if len(calls.of(operation)) > CALL_LIMIT:
                raise AssertionError(f"{operation} called more than {CALL_LIMIT} times: a loop that does not end")
            for rule in calls.rules:
                if rule["op"] == operation and rule["times"] != 0 and rule["when"](params):
                    if rule["times"] is not None:
                        rule["times"] -= 1
                    if rule["params"]:
                        params = rule["params"](dict(params))
                        break
                    if rule["error"]:
                        # The class botocore raises for this code (TransactionCanceledException, ...), as when
                        # AWS answers with it: code that catches the modeled exception must see that class.
                        error_class = client.exceptions.from_code(rule["error"])
                        raise error_class({"Error": {"Code": rule["error"], "Message": "answered by the scenario"},
                                           **rule["extra"]}, operation)
                    return rule["reply"]
            return real(client, operation, params)

        botocore.client.BaseClient._make_api_call = make_api_call
        context.undo.append(lambda: setattr(botocore.client.BaseClient, "_make_api_call", real))


def refuse_network(context) -> None:
    """Every connection out of the process fails, as on a laptop without a network."""
    real_connect, real_create = socket.socket.connect, socket.create_connection

    def refuse(*a, **k):
        raise OSError("network refused by the scenario")

    socket.socket.connect = refuse
    socket.create_connection = refuse
    context.undo.append(lambda: (setattr(socket.socket, "connect", real_connect),
                                 setattr(socket, "create_connection", real_create)))


def load(rel: str):
    """A script of the recipe as a module, loaded fresh, as `uv run python <rel>` would run it."""
    spec = importlib.util.spec_from_file_location(Path(rel).stem + "_under_test", ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_main(context, rel: str, argv: list[str]) -> None:
    """Call main(argv) of a script; keep what it printed and its exit code."""
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            load(rel).main(argv)
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
            if isinstance(e.code, str):
                print(e.code, file=sys.stderr)
    context.printed += out.getvalue()
    context.printed_err += err.getvalue()
    context.exit_code = code

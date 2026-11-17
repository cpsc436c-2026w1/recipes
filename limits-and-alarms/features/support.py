"""Helpers for the steps: a record of every AWS call, the function run in-process, captured runs of the scripts."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import socket
import sys
import time
import uuid
from pathlib import Path

import botocore.client
from botocore.exceptions import ClientError
from botocore.response import StreamingBody

ROOT = Path(__file__).resolve().parent.parent
CALL_LIMIT = 200  # a mutated loop fails at this many calls of one operation, instead of hanging


class Calls:
    """Sits where boto3 hands a call to the network (and to moto behind it).

    It writes down every call with its parameters and the Region of its client. For an operation a scenario names,
    it answers in place of AWS: an error with an error code from the API reference, a reply, or a reply that a
    function builds from the call. Everything else goes on to moto, except Invoke: moto would run the function in a
    Docker container, so an Invoke that no scenario answers fails the scenario."""

    def __init__(self):
        self.log: list[tuple[str, dict, str]] = []
        self.rules: list[dict] = []

    def answer(self, operation: str, *, when=lambda params: True, error: str = "", reply: dict | None = None,
               times: int | None = None, extra: dict | None = None, fn=None) -> None:
        # the newest rule first, so a scenario's rule goes before the defaults of environment.py
        self.rules.insert(0, {"op": operation, "when": when, "error": error, "reply": reply, "times": times,
                           "extra": extra or {}, "fn": fn})

    def of(self, operation: str) -> list[dict]:
        return [params for op, params, _ in self.log if op == operation]

    def since_mark(self, operation: str) -> list[dict]:
        """The calls of `operation` since the last script run started (a Given step's own calls are not counted)."""
        return [params for op, params, _ in self.log[getattr(self, "mark", 0):] if op == operation]

    def regions(self, operation: str) -> set[str]:
        return {region for op, _, region in self.log if op == operation}

    def install(self, context) -> None:
        real = botocore.client.BaseClient._make_api_call
        calls = self

        def make_api_call(client, operation, params):
            calls.log.append((operation, dict(params), client.meta.region_name))
            if client.meta.service_model.service_name == "budgets":
                budget_tags = budgets_tags(calls, operation, params)
                if budget_tags is not None:
                    return budget_tags
            if len(calls.of(operation)) > CALL_LIMIT:
                raise AssertionError(f"{operation} called more than {CALL_LIMIT} times: a loop that does not end")
            for rule in calls.rules:
                if rule["op"] == operation and rule["times"] != 0 and rule["when"](params):
                    if rule["times"] is not None:
                        rule["times"] -= 1
                    if rule["error"]:
                        # The class botocore raises for this code (TransactionCanceledException, ...), as when
                        # AWS answers with it: code that catches the modeled exception must see that class.
                        error_class = client.exceptions.from_code(rule["error"])
                        raise error_class({"Error": {"Code": rule["error"], "Message": "answered by the scenario"},
                                           **rule["extra"]}, operation)
                    return rule["fn"](client, params) if rule["fn"] else rule["reply"]
            if operation == "Invoke":
                raise AssertionError("Invoke reached moto, which would start a Docker container")
            return real(client, operation, params)

        botocore.client.BaseClient._make_api_call = make_api_call
        context.undo.append(lambda: setattr(botocore.client.BaseClient, "_make_api_call", real))


def budgets_tags(calls, operation, params):
    """moto keeps no tags on a budget and has no ListTagsForResource for AWS Budgets. This keeps the ResourceTags of
    CreateBudget, as AWS Budgets does, and answers ListTagsForResource from them; a budget that is not there is a
    NotFoundException, the code on the API page. Other calls return None and go on."""
    store = calls.__dict__.setdefault("budget_tags", {})
    if operation == "CreateBudget":
        store[params["Budget"]["BudgetName"]] = list(params.get("ResourceTags", []))
    elif operation == "DeleteBudget":
        store.pop(params["BudgetName"], None)
    elif operation == "ListTagsForResource":
        name = params["ResourceARN"].split("budget/")[-1]
        if name not in store:
            raise ClientError({"Error": {"Code": "NotFoundException", "Message": "budget not found"}}, operation)
        return {"ResourceTags": store[name]}
    return None


class LambdaContext:
    def __init__(self, timeout_s: int):
        self.aws_request_id = str(uuid.uuid4())
        self._end = time.monotonic() + timeout_s

    def get_remaining_time_in_millis(self) -> int:
        return int((self._end - time.monotonic()) * 1000)


def run_function_in_process(context) -> None:
    """Answer Invoke the way Lambda does for a synchronous call, without a container.

    Like Lambda: the handler `app.handler` of the deployed function runs with the function's environment variables
    and a context with a request id; what it prints becomes log events in /aws/lambda/<function>, a log group made
    on the first run; its return value is the Payload; an exception is a 200 with FunctionError "Unhandled".
    Unlike Lambda: it runs the files of main/, not the zip; the timeout, the memory size and reserved concurrency
    are not enforced (no throttles); the module is imported again for every Invoke, so nothing stays warm between
    requests as it would in a reused execution environment; InvocationType is ignored (every call is answered as a
    synchronous RequestResponse); an error payload has no stackTrace; the log stream has no START, END or REPORT
    lines; CloudWatch does not turn the log lines into metrics (main/alarm_eval.py checks the alarms against the
    same lines offline)."""
    context.invocations = []

    def invoke(client, params):
        name = params["FunctionName"]
        conf = client.get_function_configuration(FunctionName=name)
        env = (conf.get("Environment") or {}).get("Variables", {})
        old = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        out = io.StringIO()
        error = None
        try:
            spec = importlib.util.spec_from_file_location("app_in_lambda", ROOT / "main" / "app.py")
            app = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(app)
            with contextlib.redirect_stdout(out):
                try:
                    result = app.handler(json.loads(params.get("Payload") or b"{}"), LambdaContext(conf["Timeout"]))
                except Exception as e:  # noqa: BLE001  Lambda reports it as FunctionError
                    error, result = "Unhandled", {"errorMessage": str(e), "errorType": type(e).__name__}
        finally:
            for k, v in old.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        lines = [ln for ln in out.getvalue().splitlines() if ln]
        context.invocations.append({"event": json.loads(params.get("Payload") or b"{}"), "result": result,
                                    "lines": lines})
        logs = boto3_client("logs", client.meta.region_name)
        group = f"/aws/lambda/{name}"
        if not logs.describe_log_groups(logGroupNamePrefix=group)["logGroups"]:
            logs.create_log_group(logGroupName=group)
        stream = f"in-process/{uuid.uuid4().hex[:8]}"
        logs.create_log_stream(logGroupName=group, logStreamName=stream)
        if lines:
            now = int(time.time() * 1000)
            logs.put_log_events(logGroupName=group, logStreamName=stream,
                                logEvents=[{"timestamp": now, "message": ln} for ln in lines])
        body = json.dumps(result).encode()
        reply = {"StatusCode": 200, "Payload": StreamingBody(io.BytesIO(body), len(body)), "ExecutedVersion": "$LATEST"}
        if error:
            reply["FunctionError"] = error
        return reply

    context.calls.answer("Invoke", fn=invoke)


def boto3_client(service: str, region: str):
    import boto3

    return boto3.client(service, region_name=region)


def refuse_network(context) -> None:
    """Every connection out of the process fails, as on a laptop without a network."""
    real_connect, real_create = socket.socket.connect, socket.create_connection

    def refuse(*a, **k):
        raise OSError("network refused by the scenario")

    socket.socket.connect = refuse
    socket.create_connection = refuse
    context.undo.append(lambda: (setattr(socket.socket, "connect", real_connect),
                                 setattr(socket, "create_connection", real_create)))


def no_waiting(context) -> None:
    """time.sleep returns at once and is counted: fire.py waits minutes for CloudWatch, and a waiter between polls."""
    real = time.sleep
    context.sleeps = []
    time.sleep = lambda s: context.sleeps.append(s)
    context.undo.append(lambda: setattr(time, "sleep", real))


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

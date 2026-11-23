"""Stand-ins for the scenarios. Nothing here opens a network connection.

Calls sits where boto3 hands a call to the network (botocore's BaseClient._make_api_call): it writes down every call
(operation and parameters) and answers the calls a scenario names: a refusal with an error code, or a changed
answer. One change is always on: moto's Tagging API does not cover Kinesis, so GetResources is given the streams
whose tags match, as AWS gives them.
"""
from __future__ import annotations

import contextlib
import io
import runpy
import sys
import threading
import time
from pathlib import Path

import boto3
import botocore.client

ROOT = Path(__file__).resolve().parent.parent


def no_sleep():
    time.sleep = lambda s: None          # waiters and the role's 10 s: nothing to wait for in moto


class Calls:
    def __init__(self):
        self.made: list[tuple[str, dict]] = []
        self.rules: list[dict] = []
        self._lock = threading.RLock()

    def start(self):
        self._real = botocore.client.BaseClient._make_api_call
        calls = self

        def make_api_call(client, operation, params):
            with calls._lock:
                calls.made.append((operation, dict(params)))
                rule = next((r for r in calls.rules if r["operation"] == operation and r["left"] != 0
                             and r["when"](params)), None)
                if rule:
                    rule["left"] -= 1
            if rule is None:
                return calls._real(client, operation, params)
            if rule["kind"] == "refuse":
                raise client.exceptions.from_code(rule["code"])(
                    {"Error": {"Code": rule["code"], "Message": "put in by the scenario"}}, operation)
            return rule["fn"](calls._real(client, operation, params), params)

        botocore.client.BaseClient._make_api_call = make_api_call

    def stop(self):
        botocore.client.BaseClient._make_api_call = self._real

    def refuse(self, operation, code, times=-1, when=None):
        self.rules.append({"operation": operation, "kind": "refuse", "code": code, "left": times,
                           "when": when or (lambda p: True)})

    def change(self, operation, fn, times=-1, when=None):
        self.rules.append({"operation": operation, "kind": "change", "fn": fn, "left": times,
                           "when": when or (lambda p: True)})

    def names(self) -> list[str]:
        return [op for op, _ in self.made]

    def of(self, operation: str) -> list[dict]:
        return [p for op, p in self.made if op == operation]

    def kinesis_in_tagging_api(self):
        def add_streams(answer, params):
            if params.get("PaginationToken"):
                return answer
            wanted = {f["Key"]: f.get("Values", []) for f in params.get("TagFilters", [])}
            k = boto3.client("kinesis", region_name="ca-central-1")
            for name in k.list_streams()["StreamNames"]:
                tags = {t["Key"]: t["Value"] for t in k.list_tags_for_stream(StreamName=name)["Tags"]}
                if all(tags.get(key) in values for key, values in wanted.items()):
                    arn = k.describe_stream_summary(StreamName=name)["StreamDescriptionSummary"]["StreamARN"]
                    answer.setdefault("ResourceTagMappingList", []).append(
                        {"ResourceARN": arn, "Tags": [{"Key": a, "Value": b} for a, b in tags.items()]})
            return answer
        self.change("GetResources", add_streams)


def run_script(context, script: str, argv: list[str]) -> None:
    """One of the recipe's scripts, as `uv run python <script>` starts it, in this process (inside moto)."""
    out, old = io.StringIO(), sys.argv
    sys.argv = [script, *argv]
    context.mark = len(context.calls.made)
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                runpy.run_path(str(ROOT / script), run_name="__main__")
                context.exit_code = 0
            except SystemExit as e:
                context.exit_code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    finally:
        sys.argv = old
    context.printed += out.getvalue()

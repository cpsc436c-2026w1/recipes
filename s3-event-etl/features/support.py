"""Stand-ins for the scenarios. Nothing here opens a network connection.

Calls sits where boto3 hands a call to the network (botocore's BaseClient._make_api_call), writes down every call and
answers the ones a scenario names. GlueStub answers StartJobRun by running pipeline/local_load.py's load at once
(DuckDB, no PySpark), and GetJobRun with how it ended; it answers Athena's StartQueryExecution of a MERGE the same way
(without the manifests, which the sweep writes), and GetQueryExecution with SUCCEEDED.
"""
from __future__ import annotations

import itertools
import threading

import boto3
import botocore.client


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
            return rule["fn"](params)

        botocore.client.BaseClient._make_api_call = make_api_call

    def stop(self):
        botocore.client.BaseClient._make_api_call = self._real

    def answer(self, operation, fn, times=-1, when=None):
        self.rules.append({"operation": operation, "kind": "answer", "fn": fn, "left": times,
                           "when": when or (lambda p: True)})

    def of(self, operation: str) -> list[dict]:
        return [p for op, p in self.made if op == operation]


class GlueStub:
    def __init__(self, context):
        self.context, self.runs, self.queries = context, {}, {}
        self.fail_next = False
        self.ids = itertools.count(1)
        context.calls.answer("StartJobRun", self.start_job_run)
        context.calls.answer("GetJobRun", self.get_job_run)
        context.calls.answer("StartQueryExecution", self.start_query,
                             when=lambda p: p["QueryString"].startswith("MERGE INTO"))
        context.calls.answer("GetQueryExecution", self.get_query, when=lambda p: p["QueryExecutionId"] in self.queries)

    def _load(self, args_days, manifests=True):
        import local_load
        s3 = boto3.client("s3", region_name="ca-central-1")
        fail, self.fail_next = self.fail_next, False
        try:
            local_load.load(s3, self.bucket, args_days, fail_after_merge=fail, manifests=manifests)
            return {"JobRunState": "SUCCEEDED"}
        except RuntimeError as err:
            return {"JobRunState": "FAILED", "ErrorMessage": str(err)}

    def start_job_run(self, params):
        args = params["Arguments"]
        self.bucket = args["--BUCKET"]
        run = f"jr_{next(self.ids):04d}"
        self.runs[run] = {"Id": run, **self._load(args["--OPEN_DAYS"].split(","))}
        return {"JobRunId": run}

    def get_job_run(self, params):
        return {"JobRun": self.runs[params["RunId"]]}

    def start_query(self, params):
        import control
        qid = f"q-{next(self.ids):04d}"
        self.bucket = self.context.bucket
        result = self._load(control.open_days(control.now()), manifests=False)
        state = "SUCCEEDED" if result["JobRunState"] == "SUCCEEDED" else "FAILED"
        self.queries[qid] = {"QueryExecutionId": qid, "Query": params["QueryString"],
                             "WorkGroup": params.get("WorkGroup"), "Status": {"State": state},
                             "Statistics": {"DataScannedInBytes": 10_485_760}}
        return {"QueryExecutionId": qid}

    def get_query(self, params):
        return {"QueryExecution": self.queries[params["QueryExecutionId"]]}

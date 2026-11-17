"""How the steps run the recipe and what stands in for AWS. Nothing here opens a network connection.

Calls sits where boto3 hands a call to the network (botocore's BaseClient._make_api_call). It writes down every
call (operation, parameters, Region) and, for the calls a scenario names, answers in place of AWS:
- a refusal with an error code of the API reference;
- Athena: every query "succeeds", and a scenario says which rows a SELECT returns and after how many tries (moto
  has no query engine);
- Firehose PutRecordBatch: accepts the records, or rejects the ones a scenario names (FailedPutCount, as the API
  reference describes a partly failed batch).
Every other call goes on to moto.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import threading
from pathlib import Path

import botocore.client
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parent.parent
REPEAT_LIMIT = 40          # a scenario must fail, not hang: this many identical calls in a row end it


class Calls:
    def __init__(self):
        self.made: list[tuple[str, dict]] = []
        self.regions: list[str] = []
        self.services: list[str] = []
        self.rules: list[dict] = []
        self.queries: list[str] = []                    # every Athena query text, in order
        self.select_answers: list[tuple] = []           # (text the query holds, rows, tries before they show)
        self.asked: dict[str, int] = {}
        self.firehose_rejects = None                    # fn(call number, record) -> True to reject
        self.firehose_calls = 0
        self._lock = threading.Lock()

    # ---- wiring
    def start(self):
        self._real = botocore.client.BaseClient._make_api_call
        calls = self

        def make_api_call(client, operation, params):
            with calls._lock:
                calls.made.append((operation, dict(params)))
                calls.regions.append(client.meta.region_name)
                calls.services.append(client.meta.service_model.service_name)
                if len(calls.made) >= REPEAT_LIMIT and all(c == calls.made[-1] for c in calls.made[-REPEAT_LIMIT:]):
                    raise RuntimeError(f"{REPEAT_LIMIT} identical {operation} calls in a row: the recipe is waiting "
                                       f"for something that will not happen in this scenario")
                rule = next((r for r in calls.rules if r["operation"] == operation and r["left"] != 0
                             and r["when"](params)), None)
                if rule:
                    rule["left"] -= 1
            if rule is not None:
                if rule["kind"] == "change":                # the real answer, changed by the scenario
                    return rule["answer"](calls._real(client, operation, params))
                if rule["kind"] == "refuse":
                    raise client.exceptions.from_code(rule["code"])(
                        {"Error": {"Code": rule["code"], "Message": "put in by the scenario"}}, operation)
                return rule["answer"](params) if callable(rule["answer"]) else rule["answer"]
            service = client.meta.service_model.service_name
            if service == "athena" and operation in ("StartQueryExecution", "GetQueryExecution", "GetQueryResults"):
                return calls._athena(operation, params)
            if service == "firehose" and operation == "PutRecordBatch":
                return calls._put_record_batch(params)
            return calls._real(client, operation, params)

        botocore.client.BaseClient._make_api_call = make_api_call

    def stop(self):
        botocore.client.BaseClient._make_api_call = self._real

    # ---- rules a scenario sets
    def refuse(self, operation, code, times=-1, when=None):
        self.rules.append({"operation": operation, "kind": "refuse", "left": times, "code": code,
                           "when": when or (lambda p: True)})

    def answer(self, operation, answer, times=-1, when=None):
        self.rules.append({"operation": operation, "kind": "answer", "left": times, "answer": answer,
                           "when": when or (lambda p: True)})

    def change(self, operation, fn, times=-1):
        """The real answer of the call goes through fn before the recipe gets it."""
        self.rules.append({"operation": operation, "kind": "change", "left": times, "answer": fn,
                           "when": lambda p: True})

    def clear_made(self):
        self.made.clear(), self.regions.clear(), self.services.clear()

    def clear(self):
        self.made.clear(), self.regions.clear(), self.services.clear(), self.queries.clear()
        self.asked.clear(), self.select_answers.clear(), self.rules.clear()

    def select_returns(self, holds: str, rows, after_tries: int = 1):
        """A SELECT whose text holds `holds` returns no rows until it has been asked `after_tries` times, then
        `rows` (a list of dicts, or a function of the query text)."""
        self.select_answers.append((holds, rows, after_tries))

    # ---- Athena
    def _athena(self, operation, params):
        if operation == "StartQueryExecution":
            self.queries.append(params["QueryString"])
            return {"QueryExecutionId": f"q-{len(self.queries)}"}
        query = self.queries[int(params["QueryExecutionId"].split("-")[1]) - 1]
        code = "\n".join(line for line in query.splitlines() if not line.lstrip().startswith("--"))
        dml = code.lstrip().upper().startswith("SELECT")
        if operation == "GetQueryExecution":
            return {"QueryExecution": {
                "QueryExecutionId": params["QueryExecutionId"], "Status": {"State": "SUCCEEDED"},
                "StatementType": "DML" if dml else "DDL",
                "Statistics": {"DataScannedInBytes": 265 + 10 * len(self.queries),
                               "EngineExecutionTimeInMillis": 600 + len(self.queries)}}}
        rows = []
        for holds, answer, after_tries in self.select_answers:
            if holds in query:
                self.asked[holds] = self.asked.get(holds, 0) + 1
                if self.asked[holds] >= after_tries:
                    rows = answer(query) if callable(answer) else answer
                break
        if not rows:
            return {"ResultSet": {"Rows": [{"Data": [{"VarCharValue": "nothing"}]}]}}
        header = list(rows[0])
        return {"ResultSet": {"Rows": [{"Data": [{"VarCharValue": h} for h in header]}] + [
            {"Data": [({} if r[h] is None else {"VarCharValue": str(r[h])}) for h in header]} for r in rows]}}

    # ---- Firehose
    def _put_record_batch(self, params):
        self.firehose_calls += 1
        responses, failed = [], 0
        for record in params["Records"]:
            row = json.loads(record["Data"])
            if self.firehose_rejects and self.firehose_rejects(self.firehose_calls, row):
                failed += 1
                responses.append({"ErrorCode": "ServiceUnavailableException", "ErrorMessage": "Slow down."})
            else:
                responses.append({"RecordId": f"r-{len(responses)}"})
        return {"FailedPutCount": failed, "Encrypted": False, "RequestResponses": responses}

    # ---- reading what was made
    def of(self, operation: str) -> list[dict]:
        return [p for op, p in self.made if op == operation]

    def names(self) -> list[str]:
        return [op for op, _ in self.made]

    def rows_sent(self) -> list[list[dict]]:
        """The rows of each PutRecordBatch call, parsed from the bytes the function sent."""
        return [[json.loads(r["Data"]) for r in p["Records"]] for p in self.of("PutRecordBatch")]


def run(context, fn, *args, argv: list[str] | None = None, **kwargs):
    """Call fn (with sys.argv set to `argv` when given); keep what it printed, its exit code and an error."""
    out = io.StringIO()
    result, context.exit_code, context.error = None, 0, None
    old_argv = sys.argv
    if argv is not None:
        sys.argv = ["recipe", *argv]
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        try:
            result = fn(*args, **kwargs)
        except SystemExit as e:
            context.exit_code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        except Exception as e:            # noqa: BLE001  the scenario states which error it expects
            context.exit_code, context.error = 1, e
        finally:
            sys.argv = old_argv
    context.printed += out.getvalue()
    context.result = result
    if isinstance(result, int) and not isinstance(result, bool) and getattr(fn, "__name__", "") == "main":
        context.exit_code = result          # clean_up.main() returns its exit code; __main__ hands it to sys.exit
    return result


@contextlib.contextmanager
def no_waits(context):
    """The recipe's waits (a new role, a poll of Athena, a retry) are written down in context.sleeps, not waited."""
    import time

    real = time.sleep
    time.sleep = lambda s: context.sleeps.append(s)
    try:
        yield
    finally:
        time.sleep = real


def client(name: str):
    import boto3

    return boto3.client(name, region_name=os.environ.get("AWS_REGION", "ca-central-1"))


def inventory() -> dict:
    """Everything of the kinds this recipe creates, whatever its name, in the (moto) account."""
    c = client
    buckets = [b["Name"] for b in c("s3").list_buckets()["Buckets"]]
    return {
        "schedules": sorted(x["Name"] for x in c("scheduler").list_schedules()["Schedules"]),
        "alarms": sorted(a["AlarmName"] for a in c("cloudwatch").describe_alarms()["MetricAlarms"]),
        "mappings": len(c("lambda").list_event_source_mappings()["EventSourceMappings"]),
        "functions": sorted(f["FunctionName"] for f in c("lambda").list_functions()["Functions"]),
        "log groups": sorted(g["logGroupName"] for g in c("logs").describe_log_groups()["logGroups"]),
        "firehose": sorted(c("firehose").list_delivery_streams()["DeliveryStreamNames"]),
        "queues": sorted(u.rsplit("/", 1)[1] for u in c("sqs").list_queues().get("QueueUrls", [])),
        "glue": sorted(d["Name"] for d in c("glue").get_databases()["DatabaseList"]),
        "buckets": sorted(buckets),
        "objects": sorted(f"{b}/{o['Key']}" for b in buckets
                          for o in c("s3").list_objects_v2(Bucket=b).get("Contents", [])),
        "tables": sorted(c("dynamodb").list_tables()["TableNames"]),
        "roles": sorted(r["RoleName"] for r in c("iam").list_roles()["Roles"]),
    }


def stream_record(op: str, trip: str, seq: str, at: int, values: dict) -> dict:
    """One DynamoDB Streams record as Lambda hands it to the function (NEW_AND_OLD_IMAGES)."""
    image = {"trip_id": {"S": trip}}
    for name, value in values.items():
        if value not in (None, ""):
            image[name] = {"N": value} if name == "fare_quote" else {"S": value}
    d = {"ApproximateCreationDateTime": at, "SequenceNumber": seq, "Keys": {"trip_id": {"S": trip}},
         "StreamViewType": "NEW_AND_OLD_IMAGES"}
    d["OldImage" if op == "REMOVE" else "NewImage"] = image
    return {"eventName": op, "eventSource": "aws:dynamodb", "dynamodb": d}

"""How the steps run the recipe and what stands in for AWS. Nothing here opens a network connection.

Calls sits where boto3 hands a call to the network (botocore's BaseClient._make_api_call). It writes down every
call (operation, parameters, service, Region) and, for the calls a scenario names, answers in place of AWS: a
refusal with an error code of the API reference, a resource in a state moto does not have, records that a stream
refuses. Every other call goes on to moto.

run_function_here stands in for the Lambda service running a function (see features/environment.py).
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import threading
import zipfile
from pathlib import Path

import botocore.client

ROOT = Path(__file__).resolve().parent.parent
REPEAT_LIMIT = 60          # a scenario must fail, not hang: this many identical calls in a row end it
FUNCTION_RUNS: list[dict] = []       # every run of a deployed function: its name, the event, what it returned
STOPPED: set[str] = set()            # functions a scenario has stopped: their runs fail, as a broken function's do
MAPPINGS: dict[str, dict] = {}       # event source mappings the recipe made: UUID -> its settings and its Region
CHECKPOINTS: dict[str, str] = {}     # per mapping of a stream: the last sequence number handed to the function
GIVEN_UP: list[dict] = []            # batches the stand-in gave up on, as it wrote them to the on-failure queue
_MODULES: dict[bytes, object] = {}
_quiet = threading.local()           # set while the stand-in itself calls moto: such a call is not the recipe's


@contextlib.contextmanager
def own_calls():
    _quiet.on = True
    try:
        yield
    finally:
        _quiet.on = False


def _stream_event(arn: str, shard: str, records: list[dict]) -> dict:
    import base64

    return {"Records": [{"eventSource": "aws:kinesis", "eventSourceARN": arn, "eventID": f"{shard}:{r['SequenceNumber']}",
                         "kinesis": {"partitionKey": r["PartitionKey"], "sequenceNumber": r["SequenceNumber"],
                                     "data": base64.b64encode(r["Data"]).decode(),
                                     "approximateArrivalTimestamp": r["ApproximateArrivalTimestamp"].timestamp()}}
                        for r in records]}


def poll_streams() -> None:
    """What Lambda's poller does for a stream, in place of moto's (which calls the function once per record at the
    moment it is put and ignores what the function returns):

    - the new records of the shard go to the function in batches of at most BatchSize, in order;
    - a batch whose function reports a failed record (ReportBatchItemFailures) counts as done up to that record,
      and the records from it on are tried again; a function that raises fails its whole batch;
    - after MaximumRetryAttempts retries the stand-in gives up on what is left of the batch, writes the batch's
      shard and sequence numbers (not the records) to the on-failure queue, and goes on with the next batch.

    Not played: BisectBatchOnFunctionError. Lambda would split what is left of a failing batch and so lose fewer
    good records than this stand-in, which gives up on all of them. MaximumRecordAgeInSeconds is not played either.
    """
    import boto3
    from moto.awslambda.models import lambda_backends

    for uuid, m in list(MAPPINGS.items()):
        arn = m["EventSourceArn"]
        if ":kinesis:" not in arn or not m.get("Enabled", True):
            continue
        region, stream = m["region"], arn.rsplit("/", 1)[1]
        kinesis = boto3.client("kinesis", region_name=region)
        with own_calls():
            shard = kinesis.list_shards(StreamName=stream)["Shards"][0]["ShardId"]
            where = ({"ShardIteratorType": "AFTER_SEQUENCE_NUMBER", "StartingSequenceNumber": CHECKPOINTS[uuid]}
                     if uuid in CHECKPOINTS else {"ShardIteratorType": m.get("StartingPosition", "TRIM_HORIZON")})
            it = kinesis.get_shard_iterator(StreamName=stream, ShardId=shard, **where)["ShardIterator"]
            records = kinesis.get_records(ShardIterator=it, Limit=10000)["Records"]
        function = lambda_backends[arn.split(":")[4]][region].get_function(m["FunctionName"])
        size = m.get("BatchSize", 100)
        for start in range(0, len(records), size):
            batch = whole = records[start:start + size]
            failures = 0
            while batch:
                result, failed, _ = run_function_here(function, _stream_event(arn, shard, batch))
                if not failed:
                    break
                first = 0
                if "ReportBatchItemFailures" in m.get("FunctionResponseTypes", []) and result.startswith("{"):
                    # Lambda retries from the lowest sequence number reported; an empty or null itemIdentifier
                    # counts as a complete batch failure (services-kinesis-batchfailurereporting).
                    ids = [f.get("itemIdentifier") for f in json.loads(result).get("batchItemFailures", [])]
                    seqs = {r["SequenceNumber"]: i for i, r in enumerate(batch)}
                    first = 0 if (not ids or any(not x for x in ids)) else min(seqs.get(x, 0) for x in ids)
                batch = batch[first:]                       # the records before the failed one are done
                failures += 1
                limit = m.get("MaximumRetryAttempts", -1)
                assert limit >= 0 or failures < REPEAT_LIMIT, "a mapping without a retry limit retries for ever"
                if 0 <= limit < failures:
                    info = {"shardId": shard, "startSequenceNumber": batch[0]["SequenceNumber"],
                            "endSequenceNumber": batch[-1]["SequenceNumber"], "batchSize": len(batch), "streamArn": arn}
                    GIVEN_UP.append({"function": m["FunctionName"], **info})
                    queue = m.get("DestinationConfig", {}).get("OnFailure", {}).get("Destination")
                    if queue:
                        with own_calls():
                            sqs = boto3.client("sqs", region_name=region)
                            url = sqs.get_queue_url(QueueName=queue.rsplit(":", 1)[1])["QueueUrl"]
                            sqs.send_message(QueueUrl=url, MessageBody=json.dumps({
                                "requestContext": {"condition": "RetryAttemptsExhausted",
                                                   "approximateInvokeCount": failures},
                                "KinesisBatchInfo": info}))
                        GIVEN_UP[-1]["queue"] = queue.rsplit(":", 1)[1]
                    break
            CHECKPOINTS[uuid] = whole[-1]["SequenceNumber"]


def run_function_here(function, event=None):
    """In place of moto's Docker run: load the handler from the zip the recipe deployed and call it here, with the
    environment variables the recipe gave the function. Returns what moto expects: (result, failed, logs)."""
    package = function.code_bytes
    module_name, handler_name = function.handler.split(".")
    key = package + function.function_name.encode()
    if key not in _MODULES:
        folder = Path(tempfile.mkdtemp(prefix="deployed-"))
        with zipfile.ZipFile(io.BytesIO(package)) as z:
            z.extractall(folder)
        spec = importlib.util.spec_from_file_location(f"deployed_{len(_MODULES)}_{module_name}",
                                                      folder / f"{module_name}.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _MODULES[key] = module
    old = dict(os.environ)
    os.environ.update(function.environment_vars)
    # Lambda tells a function its own Region in both variables
    os.environ.update(AWS_REGION=function.region, AWS_DEFAULT_REGION=function.region)
    out = io.StringIO()
    try:
        if function.function_name in STOPPED:
            raise RuntimeError("the scenario stopped this function")
        with contextlib.redirect_stdout(out):
            result = getattr(_MODULES[key], handler_name)(event, None)
        FUNCTION_RUNS.append({"function": function.function_name, "event": event, "result": result,
                              "printed": out.getvalue(), "package": zipfile.ZipFile(io.BytesIO(package)).namelist()})
        failed = bool((result or {}).get("batchItemFailures"))
        return json.dumps(result), failed, out.getvalue()
    except Exception as err:      # noqa: BLE001  a function that raises is a failed invocation, as in Lambda
        FUNCTION_RUNS.append({"function": function.function_name, "event": event, "error": repr(err)})
        return repr(err), True, out.getvalue()
    finally:
        os.environ.clear()
        os.environ.update(old)


def deliver_waiting(mapping: dict) -> None:
    """What Lambda does when the mapping of a queue is switched on: take the messages that wait in the queue, hand
    them to the function in batches, and delete the ones the function did not report as failed. moto hands a
    message to a function only at the moment it is sent, so messages sent while the mapping was off stay put."""
    import boto3
    from moto.awslambda.models import lambda_backends

    region, account = mapping["FunctionArn"].split(":")[3:5]
    function = lambda_backends[account][region].get_function(mapping["FunctionArn"])
    sqs = boto3.client("sqs", region_name=region)
    url = sqs.get_queue_url(QueueName=mapping["EventSourceArn"].rsplit(":", 1)[1])["QueueUrl"]
    while True:
        messages = sqs.receive_message(QueueUrl=url, MaxNumberOfMessages=mapping.get("BatchSize", 10),
                                       AttributeNames=["All"]).get("Messages", [])
        if not messages:
            return
        event = {"Records": [{"messageId": m["MessageId"], "receiptHandle": m["ReceiptHandle"], "body": m["Body"],
                              "attributes": m["Attributes"], "eventSource": "aws:sqs",
                              "eventSourceARN": mapping["EventSourceArn"]} for m in messages]}
        result, _, _ = run_function_here(function, event)
        failed = {f["itemIdentifier"] for f in (json.loads(result) or {}).get("batchItemFailures", [])}
        for m in messages:
            if m["MessageId"] not in failed:
                sqs.delete_message(QueueUrl=url, ReceiptHandle=m["ReceiptHandle"])


class Calls:
    def __init__(self):
        self.made: list[tuple[str, dict]] = []
        self.regions: list[str] = []
        self.services: list[str] = []
        self.rules: list[dict] = []
        self._lock = threading.RLock()

    def start(self):
        self._real = botocore.client.BaseClient._make_api_call
        calls = self

        def make_api_call(client, operation, params):
            if getattr(_quiet, "on", False):
                return calls._real(client, operation, params)
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
            if rule is None or rule["kind"] == "change":
                if rule is None:
                    answer = calls._real(client, operation, params)
                else:
                    sent = rule["before"](params) if rule.get("before") else params
                    real = calls._real(client, operation, sent) if sent is not None else {"FailedRecordCount": 0,
                                                                                          "Records": []}
                    answer = rule["answer"](real, params)       # before() == None: nothing of the call reaches moto
                if operation == "CreateEventSourceMapping":
                    MAPPINGS[answer["UUID"]] = {**params, "region": client.meta.region_name}
                elif operation == "UpdateEventSourceMapping" and answer["UUID"] in MAPPINGS:
                    MAPPINGS[answer["UUID"]].update(params)
                elif operation == "DeleteEventSourceMapping":
                    MAPPINGS.pop(params["UUID"], None)
                if operation == "UpdateEventSourceMapping" and params.get("Enabled") and ":sqs:" in answer.get(
                        "EventSourceArn", ""):
                    deliver_waiting(answer)       # Lambda starts to poll the queue once its mapping is switched on
                if operation in ("PutRecords", "PutRecord", "CreateEventSourceMapping", "UpdateEventSourceMapping"):
                    poll_streams()                # Lambda's poller takes what is new on the stream
                return answer
            if rule["kind"] == "refuse":
                raise client.exceptions.from_code(rule["code"])(
                    {"Error": {"Code": rule["code"], "Message": "put in by the scenario"}}, operation)
            return rule["answer"](params) if callable(rule["answer"]) else rule["answer"]

        botocore.client.BaseClient._make_api_call = make_api_call

    def stop(self):
        botocore.client.BaseClient._make_api_call = self._real

    def _add(self, operation, kind, times, when, **more):
        self.rules.append({"operation": operation, "kind": kind, "left": times, "when": when or (lambda p: True), **more})

    def refuse(self, operation, code, times=-1, when=None):
        self._add(operation, "refuse", times, when, code=code)

    def answer(self, operation, answer, times=-1, when=None):
        self._add(operation, "answer", times, when, answer=answer)

    def change(self, operation, fn, times=-1, when=None, before=None):
        """The real answer of the call goes through fn(answer, parameters) before the recipe gets it. With `before`,
        the call that reaches moto carries before(parameters) instead: a refused record is then not on the stream."""
        self._add(operation, "change", times, when, answer=fn, before=before)

    def clear(self):
        self.made.clear(), self.regions.clear(), self.services.clear(), self.rules.clear()

    def clear_made(self):
        self.made.clear(), self.regions.clear(), self.services.clear()

    def of(self, operation: str, service: str | None = None) -> list[dict]:
        return [p for (op, p), s in zip(self.made, self.services) if op == operation and service in (None, s)]

    def names(self) -> list[str]:
        return [op for op, _ in self.made]

    def pairs(self) -> list[tuple[str, str]]:
        return list(zip(self.services, self.names()))


@contextlib.contextmanager
def no_waits(context):
    """The recipe's waits are written down in context.sleeps, not waited, and the clock the recipe reads
    (time.time) moves on by each of them: a wait of 5 s takes no time and still counts as 5 s."""
    import time

    real_sleep, real_time, skipped = time.sleep, time.time, [0.0]

    def sleep(seconds):
        context.sleeps.append(round(seconds, 3))
        skipped[0] += max(0.0, seconds)

    time.sleep, time.time = sleep, lambda: real_time() + skipped[0]
    try:
        yield
    finally:
        time.sleep, time.time = real_sleep, real_time


def load(name: str, rel: str):
    """A module by its path: main/run.py and the two extensions' run.py are all called run."""
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
    context.result = result
    return result


SCRIPTS = {"main/run.py": "main/run.py", "windowed aggregates": "ext-windowed-aggregates/run.py",
           "fan-out": "ext-fan-out/run.py"}


def run_script(context, which: str, args: str):
    module = load("recipe_" + which.replace("/", "_").replace("-", "_").replace(" ", "_").removesuffix(".py"),
                  SCRIPTS[which])
    with no_waits(context):
        return run(context, module.main, args.split())


def run_clean_up(context):
    import runpy

    sys.modules.pop("clean_up", None)
    with no_waits(context):
        return run(context, runpy.run_path, str(ROOT / "clean_up.py"), run_name="__main__")


def client(name: str):
    import boto3

    return boto3.client(name, region_name=os.environ.get("AWS_REGION", "ca-central-1"))


def inventory() -> dict:
    """Everything of the kinds this recipe creates, whatever its name, in the (moto) account."""
    c = client
    return {
        "functions": sorted(f["FunctionName"] for f in c("lambda").list_functions()["Functions"]),
        "mappings": len(c("lambda").list_event_source_mappings()["EventSourceMappings"]),
        "tables": sorted(c("dynamodb").list_tables()["TableNames"]),
        "streams": sorted(c("kinesis").list_streams()["StreamNames"]),
        "queues": sorted(u.rsplit("/", 1)[1] for u in c("sqs").list_queues().get("QueueUrls", [])),
        "topics": sorted(t["TopicArn"].rsplit(":", 1)[1] for t in c("sns").list_topics()["Topics"]),
        "roles": sorted(r["RoleName"] for r in c("iam").list_roles()["Roles"]),
        "log groups": sorted(g["logGroupName"] for g in c("logs").describe_log_groups()["logGroups"]),
    }


def items(table: str) -> list[dict]:
    """Every item of a table in moto, with DynamoDB's types taken off."""
    found, kwargs = [], {"TableName": table, "ConsistentRead": True}
    while True:
        page = client("dynamodb").scan(**kwargs)
        found += [{k: next(iter(v.values())) for k, v in item.items()} for item in page["Items"]]
        if "LastEvaluatedKey" not in page:
            return found
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def lag_rows(printed: str) -> dict[str, tuple[float, float]]:
    """The table of the lag main/run.py prints: part -> (median s, p95 s)."""
    import re

    return {m.group(1).strip(): (float(m.group(2)), float(m.group(3)))
            for m in re.finditer(r"^(.+?)\s+(-?\d+\.\d{3})\s+(-?\d+\.\d{3})$", printed, flags=re.M)}

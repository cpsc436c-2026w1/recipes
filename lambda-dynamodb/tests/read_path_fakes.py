"""Stand-ins for ext-read-path's tests and scenarios: the API's URL (main/handler.py inside moto, with a request id
and a REPORT line per call), the function's log group and configuration, and a clock that moves only when asked.
Nothing here reaches AWS."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

RECIPE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(RECIPE / "ext-read-path"), str(RECIPE / "scripts")]


class Clock:
    def __init__(self, start: datetime = datetime(2026, 11, 23, 10, 0, tzinfo=timezone.utc)):
        self.t = start
        self.slept = 0.0

    def now(self) -> datetime:
        return self.t

    def sleep(self, s: float) -> None:
        self.slept += s
        self.t += timedelta(seconds=s)

    def monotonic(self) -> float:
        return (self.t - datetime(2026, 1, 1, tzinfo=timezone.utc)).total_seconds()


class FakeLogs:
    def __init__(self):
        self.events: list[dict] = []

    def filter_log_events(self, **kw):
        return {"events": [e for e in self.events if kw["startTime"] <= e["timestamp"] <= kw["endTime"]]}


class FakeLambda:
    """get/update_function_configuration with LastUpdateStatus InProgress for `reads_to_finish` reads."""

    def __init__(self, env: dict, reads_to_finish: int = 1):
        self.env, self.reads_to_finish, self.updates, self._left = dict(env), reads_to_finish, [], 0
        self.status_reads: list[str] = []
        self.journal: list[tuple] = []          # ("update", env) and ("status", state), in order

    def get_function_configuration(self, FunctionName):
        status = "Successful"
        if self._left > 0:
            self._left -= 1
            status = "InProgress"
        self.status_reads.append(status)
        self.journal.append(("status", status))
        return {"Environment": {"Variables": dict(self.env)}, "LastUpdateStatus": status}

    def update_function_configuration(self, FunctionName, Environment):
        self.env = dict(Environment["Variables"])
        self.updates.append(dict(self.env))
        self.journal.append(("update", dict(self.env)))
        self._left = self.reads_to_finish
        return {}

    def get_function_url_config(self, FunctionName):
        return {"FunctionUrl": "https://fake-url.lambda-url.ca-central-1.on.aws/"}


class FakeApi:
    """The function behind its URL: main/handler.py against moto. Each call gets a request id (the
    x-amzn-RequestId header) and a REPORT line in FakeLogs; the first call and the first after a configuration
    change are cold, as is any call while `cold_next` is set."""

    def __init__(self, clock: Clock, logs: FakeLogs, lam: FakeLambda | None = None, fail: set[int] = frozenset(),
                 break_at: int | None = None):
        spec = importlib.util.spec_from_file_location("handler_rp", RECIPE / "main" / "handler.py")
        self.handler = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.handler)
        self.clock, self.logs, self.lam, self.fail = clock, logs, lam, set(fail)
        self.calls: list[dict] = []
        self.spans: list[tuple[datetime, datetime]] = []     # (sent, answered) on the fake clock
        self.break_at = break_at
        self.cold_next, self._seen_env = True, None
        self.caller_ms = 100.0

    def __call__(self, url: str, body: dict) -> dict:
        n = len(self.calls)
        if n == self.break_at:
            raise ConnectionError("the fake URL broke on purpose")
        self.calls.append(body)
        sent = self.clock.now()
        if self.lam is not None:
            self.lam.journal.append(("call", body.get("action")))
        if self.lam is not None and self.lam.env != self._seen_env:
            self.cold_next, self._seen_env = True, dict(self.lam.env)
        if n in self.fail:
            self.clock.sleep(0.05)
            return {"caller_ms": 50.0, "status": 429, "request_id": None, "reply": {"Reason": "throttled"}}
        rid = str(uuid.uuid4())
        with contextlib.redirect_stdout(io.StringIO()):       # the handler's log line goes to its log, not here
            out = self.handler.lambda_handler({"body": json.dumps(body)}, type("C", (), {"aws_request_id": rid})())
        cold, self.cold_next = self.cold_next, False
        line = (f"REPORT RequestId: {rid}\tDuration: {316.02 if cold else 24.70} ms\tBilled Duration: "
                f"{810 if cold else 25} ms\tMemory Size: 128 MB\tMax Memory Used: 92 MB\t"
                + ("Init Duration: 493.18 ms\t" if cold else ""))
        self.logs.events.append({"timestamp": int(self.clock.now().timestamp() * 1000), "message": line})
        self.clock.sleep(self.caller_ms / 1000)
        self.spans.append((sent, self.clock.now()))
        return {"caller_ms": self.caller_ms + (500 if cold else 0), "status": out["statusCode"], "request_id": rid,
                "reply": json.loads(out["body"])}


class Sess:
    """A boto3 session stand-in: moto for DynamoDB, the fakes for Lambda and CloudWatch Logs."""

    def __init__(self, ddb, lam, logs):
        self._c = {"dynamodb": ddb, "lambda": lam, "logs": logs}

    def client(self, service, **_):
        return self._c[service]


def tables(ddb, prefix: str = "recipe") -> None:
    """The dynamodb recipe's two tables with SIM01: 50 trips and 50 past trips of riders r-00 to r-04."""
    ddb.create_table(TableName=f"{prefix}-trips", BillingMode="PAY_PER_REQUEST",
                     KeySchema=[{"AttributeName": "trip_id", "KeyType": "HASH"}],
                     AttributeDefinitions=[{"AttributeName": "trip_id", "AttributeType": "S"}])
    ddb.create_table(TableName=f"{prefix}-rider-trips", BillingMode="PAY_PER_REQUEST",
                     KeySchema=[{"AttributeName": "rider_id", "KeyType": "HASH"},
                                {"AttributeName": "requested_at", "KeyType": "RANGE"}],
                     AttributeDefinitions=[{"AttributeName": "rider_id", "AttributeType": "S"},
                                           {"AttributeName": "requested_at", "AttributeType": "S"}])
    for i in range(50):
        ddb.put_item(TableName=f"{prefix}-trips", Item={"trip_id": {"S": f"t-{i:04d}"}, "status": {"S": "requested"},
                                                         "pickup_zone": {"S": "Midtown"}, "fare_quote": {"N": str(12 + i % 30)}})
        ddb.put_item(TableName=f"{prefix}-rider-trips",
                     Item={"rider_id": {"S": f"r-{i % 5:02d}"}, "requested_at": {"S": f"2026-10-{1 + i // 5:02d}T11:15:00Z"},
                           "trip_id": {"S": f"h-{i:04d}"}, "pickup_zone": {"S": "SoHo"}})


class _Raw:
    def __init__(self, body: bytes):
        self.body = body

    def stream(self, **_):
        yield self.body


def throttle_once(client, n: int) -> dict:
    """Make DynamoDB answer the first attempt of `n` different UpdateItem calls with a throttle (HTTP 400,
    ProvisionedThroughputExceededException); the retry of each goes through to moto."""
    from botocore.awsrequest import AWSResponse
    state = {"left": n, "seen": set(), "throttled": 0}

    def hook(request, **_):
        body = request.body if isinstance(request.body, bytes) else str(request.body).encode()
        if state["left"] > 0 and body not in state["seen"]:
            state["seen"].add(body)
            state["left"] -= 1
            state["throttled"] += 1
            err = json.dumps({"__type": "com.amazonaws.dynamodb.v20120810#ProvisionedThroughputExceededException",
                              "message": "throttled by the test"}).encode()
            return AWSResponse(request.url, 400, {"x-amzn-requestid": "test", "content-type": "application/x-amz-json-1.0"},
                               _Raw(err))
        return None

    client.meta.events.register_first("before-send.dynamodb.UpdateItem", hook)
    return state

"""How the steps run the recipe and what stands in for AWS and for the model. Nothing here opens a network
connection.

Calls sits where boto3 hands a call to the network (botocore's BaseClient._make_api_call). It writes down every
call (operation, parameters, service, Region) and, for the calls a scenario names, answers in place of AWS: a
refusal with an error code of the API reference, a resource in a state moto does not have, records that a stream
refuses, the files Firehose wrote, the rows Athena returns. Every other call goes on to moto.

function_url stands in for a Lambda function URL with AWS_IAM auth: it refuses a request that is not signed for
Lambda in ca-central-1, and runs a signed one through the handler of the package the recipe deployed.

Gateway stands in for the course LLM gateway: an OpenAI client whose model answers what the scenario scripted.
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
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from types import SimpleNamespace as NS

import botocore.client

ROOT = Path(__file__).resolve().parent.parent
ACCOUNT = "123456789012"   # moto's account
REPEAT_LIMIT = 150         # a scenario must fail, not hang: this many identical calls in a row end it
URL_REQUESTS: list[dict] = []        # every request to a function URL: method, path, signed or not, status
GATEWAYS: list["Gateway"] = []       # every gateway client the recipe made
SCRIPT: list[dict] = []              # what the model answers, one entry per model call
_MODULES: dict[bytes, object] = {}
_REAL = {}


# ---- the function URL

def _deployed(function):
    """The handler module of the package the recipe deployed, loaded once per scenario."""
    package = function.code_bytes
    if package not in _MODULES:
        folder = Path(tempfile.mkdtemp(prefix="deployed-"))
        with zipfile.ZipFile(io.BytesIO(package)) as z:
            z.extractall(folder)
        name = function.handler.split(".")[0]
        spec = importlib.util.spec_from_file_location(f"deployed_{len(_MODULES)}_{name}", folder / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _MODULES[package] = module
    return _MODULES[package]


class _Response:
    def __init__(self, body: str):
        self._body = body.encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def function_url(request, timeout=None):
    """In place of urllib.request.urlopen, for the URLs moto gave a function."""
    import boto3
    from moto.awslambda.models import lambda_backends

    url = urllib.parse.urlsplit(request.full_url)
    lam = boto3.client("lambda", region_name="ca-central-1")
    found = None
    for f in lam.list_functions()["Functions"]:
        with contextlib.suppress(lam.exceptions.ResourceNotFoundException):
            if urllib.parse.urlsplit(lam.get_function_url_config(FunctionName=f["FunctionName"])["FunctionUrl"]
                                     ).netloc == url.netloc:
                found = f["FunctionName"]
    if found is None:
        raise urllib.error.URLError(f"the scenario has no function behind {request.full_url}")
    auth = request.get_header("Authorization") or ""
    signed = auth.startswith("AWS4-HMAC-SHA256") and "/ca-central-1/lambda/aws4_request" in auth
    seen = {"function": found, "method": request.get_method(), "path": url.path, "signed": signed}
    with contextlib.suppress(ValueError, AttributeError):
        seen["updates"] = len(json.loads(request.data or b"{}").get("updates", []))
    URL_REQUESTS.append(seen)
    if not signed:
        seen["status"] = 403
        raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, io.BytesIO(b'{"Message":"Forbidden"}'))
    function = lambda_backends[ACCOUNT]["ca-central-1"].get_function(found)
    event = {"requestContext": {"http": {"method": request.get_method()}}, "rawPath": url.path or "/",
             "queryStringParameters": dict(urllib.parse.parse_qsl(url.query)) or None,
             "body": request.data.decode() if request.data else None, "isBase64Encoded": False}
    old = dict(os.environ)
    os.environ.update(function.environment_vars)
    os.environ.update(AWS_REGION=function.region, AWS_DEFAULT_REGION=function.region)   # as Lambda sets them
    log = io.StringIO()
    try:
        with contextlib.redirect_stdout(log):          # what the function prints goes to its log group, not to you
            result = _deployed(function).handler(event, NS(aws_request_id=f"request-{len(URL_REQUESTS)}"))
    finally:
        os.environ.clear()
        os.environ.update(old)
    seen.update(log=log.getvalue(), status=result["statusCode"], package=zipfile.ZipFile(io.BytesIO(function.code_bytes)).namelist())
    if result["statusCode"] >= 400:
        raise urllib.error.HTTPError(request.full_url, result["statusCode"], "error", {},
                                     io.BytesIO(result["body"].encode()))
    return _Response(result["body"])


# ---- the course LLM gateway

class Gateway:
    """An OpenAI client as the recipe makes it. Each model call takes the next entry of SCRIPT:
    {"tool": "count_updates", "arguments": {...}} is a tool call, {"reply": "..."} a reply, {"cap_used_up": True} a
    reply with no text and finish_reason "length", {"cut_tool_call": True} a tool call cut in its arguments with
    finish_reason "length"; in a reply, {result} is the number of the last tool result. With
    the script used up, the model answers with the last entry again."""

    def __init__(self, **kw):
        self.made_with, self.calls = kw, []
        self.chat = NS(completions=NS(create=self._create))
        GATEWAYS.append(self)

    def _create(self, **kw):
        self.calls.append(kw)
        step = SCRIPT[min(len(self.calls), len(SCRIPT)) - 1]
        usage = NS(prompt_tokens=step.get("tokens_in", 120), completion_tokens=step.get("tokens_out", 15))
        if step.get("cut_tool_call"):        # a tool call that stopped at max_tokens inside its arguments
            usage.completion_tokens = kw["max_tokens"]
            call = NS(id=f"call-{len(self.calls)}", type="function",
                      function=NS(name="count_updates", arguments='{"start": "2026-11-12T15:0'))
            return NS(choices=[NS(message=NS(content="", tool_calls=[call]), finish_reason="length")], usage=usage)
        if "tool" in step:
            call = NS(id=f"call-{len(self.calls)}", type="function",
                      function=NS(name=step["tool"], arguments=json.dumps(step["arguments"])))
            return NS(choices=[NS(message=NS(content="", tool_calls=[call]))], usage=usage)
        if step.get("cap_used_up"):          # a reply that stopped at max_tokens before any text
            usage.completion_tokens = kw["max_tokens"]
            return NS(choices=[NS(message=NS(content="", tool_calls=None), finish_reason="length")], usage=usage)
        results = [json.loads(m["content"]) for m in kw["messages"] if m["role"] == "tool"]
        last = results[-1].get("updates", "?") if results else "?"
        return NS(choices=[NS(message=NS(content=step["reply"].replace("{result}", str(last)), tool_calls=None))],
                  usage=usage)


def install_stand_ins():
    import time

    import openai

    _REAL["urlopen"], _REAL["OpenAI"] = urllib.request.urlopen, openai.OpenAI
    _REAL["sleep"], _REAL["time"] = time.sleep, time.time
    time.sleep, time.time = _sleep, _time
    urllib.request.urlopen = function_url
    openai.OpenAI = Gateway


def reset():
    URL_REQUESTS.clear(), GATEWAYS.clear(), SCRIPT.clear(), _MODULES.clear(), WAITS.clear()
    for name in ("handler", "genai", "operational", "lakehouse"):
        module = sys.modules.get(name)
        if module is not None and hasattr(module, "_client"):
            module._client = None          # a function makes its client on first use, as in a new environment


# ---- boto3 calls

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
            if rule is None:
                return calls._real(client, operation, params)
            if rule["kind"] == "change":
                return rule["answer"](calls._real(client, operation, params), params)
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

    def change(self, operation, fn, times=-1, when=None):
        """The real answer of the call goes through fn(answer, parameters) before the recipe gets it."""
        self._add(operation, "change", times, when, answer=fn)

    def clear_made(self):
        self.made.clear(), self.regions.clear(), self.services.clear()

    def of(self, operation: str, service: str | None = None) -> list[dict]:
        return [p for (op, p), s in zip(self.made, self.services) if op == operation and service in (None, s)]

    def names(self) -> list[str]:
        return [op for op, _ in self.made]

    def pairs(self) -> list[tuple[str, str]]:
        return list(zip(self.services, self.names()))


WAITS: list[float] = []                  # every wait of the recipe in this scenario, in seconds
_CLOCK = {"on": False, "skipped": 0.0}


def _sleep(seconds):
    if not _CLOCK["on"]:
        return _REAL["sleep"](seconds)
    WAITS.append(round(seconds, 3))
    _CLOCK["skipped"] += max(0.0, seconds)
    _REAL["sleep"](0.05)


def _time():
    return _REAL["time"]() + (_CLOCK["skipped"] if _CLOCK["on"] else 0.0)


@contextlib.contextmanager
def no_waits(context):
    """While the recipe runs, its waits are written down in WAITS (context.sleeps) and cut to a twentieth of a
    second (moto finishes an export in a thread of its own, and needs a moment), and the clock the recipe reads
    moves on by each wait in full.

    time.sleep and time.time are replaced once, before any file of the recipe is imported (install_stand_ins):
    several functions of the recipe take `sleep=time.sleep` as a default, which Python fixes at import. Replaced
    only while a run is on, such a function would keep the real sleep and a scenario would wait for minutes."""
    _CLOCK.update(on=True, skipped=0.0)
    try:
        yield
    finally:
        _CLOCK["on"] = False


def run(context, fn, *args, **kwargs):
    """Call fn; keep what it printed, its exit code (0 when it returned) and an error it raised."""
    out = io.StringIO()
    result, context.exit_code, context.error = None, 0, None
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        try:
            result = fn(*args, **kwargs)
            if isinstance(result, int) and not isinstance(result, bool):
                context.exit_code = result
        except SystemExit as e:
            if isinstance(e.code, str):                    # sys.exit("message"): Python prints it and exits with 1
                print(e.code)
            context.exit_code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        except Exception as e:            # noqa: BLE001  the scenario states which error it expects
            context.exit_code, context.error = 1, e
    context.printed += out.getvalue()
    context.last = out.getvalue()
    context.result = result
    return result


def run_script(context, script: str, args: str | list[str]):
    """One of the recipe's scripts, as `uv run python <script> <args>` starts it: its main() with these options."""
    import runpy

    argv = args.split() if isinstance(args, str) else args
    old = sys.argv
    sys.argv = [script, *argv]
    try:
        with no_waits(context):
            return run(context, runpy.run_path, str(ROOT / script), run_name="__main__")
    finally:
        sys.argv = old


def client(name: str):
    import boto3

    return boto3.client(name, region_name="ca-central-1")


def inventory() -> dict:
    """Everything of the kinds this recipe creates, whatever its name, in the (moto) account."""
    c = client
    return {
        "streams": sorted(c("kinesis").list_streams()["StreamNames"]),
        "firehose": sorted(c("firehose").list_delivery_streams()["DeliveryStreamNames"]),
        "tables": sorted(c("dynamodb").list_tables()["TableNames"]),
        "functions": sorted(f["FunctionName"] for f in c("lambda").list_functions()["Functions"]),
        "log groups": sorted(g["logGroupName"] for g in c("logs").describe_log_groups()["logGroups"]),
        "buckets": sorted(b["Name"] for b in c("s3").list_buckets()["Buckets"]),
        "glue": sorted(d["Name"] for d in c("glue").get_databases()["DatabaseList"]),
        "workgroups": sorted(w["Name"] for w in c("athena").list_work_groups()["WorkGroups"] if w["Name"] != "primary"),
        "roles": sorted(r["RoleName"] for r in c("iam").list_roles()["Roles"]),
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


def policy(role: str) -> list[dict]:
    doc = client("iam").get_role_policy(RoleName=role, PolicyName="least-privilege")["PolicyDocument"]
    return (json.loads(doc) if isinstance(doc, str) else doc)["Statement"]

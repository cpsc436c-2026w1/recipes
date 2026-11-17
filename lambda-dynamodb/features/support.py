"""How the steps load the handler and run the recipe's scripts. Nothing here reaches AWS."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from harness import FAILING, FAKE_AWS, LOGGING, PYTHON3, RECIPE, commands, put_tools, script_env

TIMEOUT_S = 120        # a script that waits for ever fails its scenario instead of hanging the run


# ---- the handler inside moto

def load_handler(context, prefix: str = "recipe"):
    """main/handler.py loaded inside moto, with the three tables of the dynamodb recipe (same names, same keys).
    Every DynamoDB call the handler makes is written to context.ddb_calls as (operation, parameters)."""
    import boto3
    from moto import mock_aws

    os.environ["PREFIX"] = prefix
    context.moto = mock_aws()
    context.moto.start()
    context.prefix = prefix
    ddb = boto3.client("dynamodb")
    for name, keys in ((f"{prefix}-trips", [("trip_id", "HASH")]),
                       (f"{prefix}-rider-trips", [("rider_id", "HASH"), ("requested_at", "RANGE")]),
                       (f"{prefix}-drivers", [("driver_id", "HASH")])):
        ddb.create_table(TableName=name, BillingMode="PAY_PER_REQUEST",
                         KeySchema=[{"AttributeName": k, "KeyType": t} for k, t in keys],
                         AttributeDefinitions=[{"AttributeName": k, "AttributeType": "S"} for k, _ in keys])
    spec = importlib.util.spec_from_file_location("handler", RECIPE / "main" / "handler.py")
    context.handler = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(context.handler)
    context.ddb_calls = []

    def note(params, model, **_):
        context.ddb_calls.append((model.name, dict(params)))

    for client in (context.handler.ddb.meta.client, context.handler.client):
        client.meta.events.register("provide-client-params.dynamodb.*", note)
    context.ddb = ddb
    return context.handler


def call(context, **event):
    """Call the handler the way a function URL does: the request is the JSON body. Keeps status and reply."""
    context.ddb_calls.clear()
    reply = context.handler.lambda_handler({"body": json.dumps(event)})
    context.status, context.reply = reply["statusCode"], json.loads(reply["body"])
    return context.reply


def item(context, table: str, **key) -> dict | None:
    """One item of a table in moto, with DynamoDB's types taken off; None when it is not there."""
    found = context.ddb.get_item(TableName=f"{context.prefix}-{table}", ConsistentRead=True,
                                 Key={k: {"S": v} for k, v in key.items()}).get("Item")
    return None if found is None else {k: next(iter(v.values())) for k, v in found.items()}


# ---- the scripts

@dataclass
class Run:
    script: str
    code: int
    out: str
    err: str
    calls: list[str] = field(default_factory=list)       # fake runs: every call the script made to aws
    regions: list[str] = field(default_factory=list)     # fake runs: AWS_REGION of each call
    curls: list[str] = field(default_factory=list)       # fake runs: the arguments of each curl call
    sleeps: list[str] = field(default_factory=list)      # fake runs: the argument of each sleep
    handler_runs: list[str] = field(default_factory=list)  # fake runs: python3 ... handler.py

    @property
    def cmds(self) -> list[str]:
        return commands(self.out)

    def step(self, n: str) -> str:
        m = re.search(rf"^== {re.escape(n)}\. .*?$(.*?)(?=^== |\Z)", self.out, flags=re.M | re.S)
        assert m, f"the run printed no step {n}:\n{self.out[-2000:]}"
        return m.group(1)


def _lines(path: Path) -> list[str]:
    return path.read_text().splitlines() if path.exists() else []


def dry(context, script: str, **extra) -> Run:
    """DRY=1; a call to aws or curl exits 99 and writes a line the steps look for."""
    bin_dir = put_tools(context.tmp / "bin", aws=FAILING, curl=FAILING)
    env = script_env(context.tmp) | {"DRY": "1", "PATH": f"{bin_dir}:{os.environ['PATH']}"} | context.env | extra
    p = subprocess.run(["bash", str(RECIPE / script)], env=env, capture_output=True, text=True, timeout=TIMEOUT_S)
    context.run = Run(script, p.returncode, p.stdout, p.stderr)
    return context.run


def fake(context, script: str, unset: tuple = (), **extra) -> Run:
    """For real (no DRY), against the stand-ins of tests/harness.py: aws, curl, sleep and a python3 that does not
    run the handler."""
    bin_dir = put_tools(context.tmp / "bin", aws=FAKE_AWS, curl=LOGGING, sleep=LOGGING, python3=PYTHON3)
    log = context.tmp / "aws.log"
    for path in (log, Path(str(log) + ".region"), Path(str(log) + ".busy")):
        path.write_text("")
    for tool in ("curl", "sleep", "python3"):
        (context.tmp / f"tool.log.{tool}").unlink(missing_ok=True)
    env = script_env(context.tmp) | {"PATH": f"{bin_dir}:{os.environ['PATH']}", "FAKE_AWS_LOG": str(log),
                                     "TOOL_LOG": str(context.tmp / "tool.log"), "AWS_REGION": "ca-central-1"}
    env |= context.env | extra
    for name in unset:
        env.pop(name, None)
    p = subprocess.run(["bash", str(RECIPE / script)], env=env, capture_output=True, text=True, timeout=TIMEOUT_S)
    context.run = Run(script, p.returncode, p.stdout, p.stderr, _lines(log), _lines(Path(str(log) + ".region")),
                      _lines(context.tmp / "tool.log.curl"), _lines(context.tmp / "tool.log.sleep"),
                      _lines(context.tmp / "tool.log.python3"))
    return context.run


def policy_of(run: Run) -> dict:
    """The policy document that the put-role-policy command of a run carried."""
    source = next((c for c in run.calls if "iam put-role-policy" in c), None) or run.out.split("aws iam put-role-policy", 1)[1]
    return json.JSONDecoder().raw_decode(source.split("--policy-document ", 1)[1])[0]

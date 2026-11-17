"""Offline harness: moto for the handler, DRY=1 for the bash scenarios. No AWS account, no network."""

import importlib.util
import os
import subprocess
from pathlib import Path

import pytest

RECIPE = Path(__file__).resolve().parents[1]

os.environ.update(AWS_ACCESS_KEY_ID="testing", AWS_SECRET_ACCESS_KEY="testing", AWS_SESSION_TOKEN="testing",
                  AWS_DEFAULT_REGION="ca-central-1", AWS_REGION="ca-central-1", PREFIX="recipe")
os.environ.pop("AWS_PROFILE", None)
os.environ.pop("AWS_DEFAULT_PROFILE", None)


def _table(ddb, name, keys):
    ddb.create_table(
        TableName=name, BillingMode="PAY_PER_REQUEST",
        KeySchema=[{"AttributeName": k, "KeyType": t} for k, t in keys],
        AttributeDefinitions=[{"AttributeName": k, "AttributeType": "S"} for k, _ in keys],
    )


@pytest.fixture(autouse=True)
def _no_profile(monkeypatch):
    """No test depends on the caller's profile: a profile set in the shell could point a test at a real account."""
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_PROFILE", raising=False)


@pytest.fixture
def handler():
    """main/handler.py loaded inside moto, with the three tables of the dynamodb recipe and a few items."""
    import boto3
    from moto import mock_aws

    with mock_aws():
        ddb = boto3.client("dynamodb")
        _table(ddb, "recipe-trips", [("trip_id", "HASH")])
        _table(ddb, "recipe-rider-trips", [("rider_id", "HASH"), ("requested_at", "RANGE")])
        _table(ddb, "recipe-drivers", [("driver_id", "HASH")])
        for i in range(3):
            ddb.put_item(TableName="recipe-trips", Item={"trip_id": {"S": f"t-{i:04d}"}, "status": {"S": "requested"},
                                                         "fare_quote": {"N": str(20 + i)}})
            ddb.put_item(TableName="recipe-rider-trips",
                         Item={"rider_id": {"S": "r-03"}, "requested_at": {"S": f"2026-10-0{i + 1}T08:00:00Z"},
                               "trip_id": {"S": f"h-{i}"}})
        for d in ("d-1", "d-2"):
            ddb.put_item(TableName="recipe-drivers", Item={"driver_id": {"S": d}, "status": {"S": "available"}})
        spec = importlib.util.spec_from_file_location("handler", RECIPE / "main" / "handler.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        yield module


@pytest.fixture
def dry(tmp_path):
    """dry(script, **env) -> stdout of `bash <script>` with DRY=1; any call to aws, curl or sleep > 0 fails the test."""
    fake = tmp_path / "bin"
    fake.mkdir()
    for tool in ("aws", "curl"):
        (fake / tool).write_text(f"#!/bin/sh\necho \"{tool} was called under DRY=1: $*\" >&2\nexit 99\n")
        (fake / tool).chmod(0o755)

    def _run(script, **extra):
        env = {k: v for k, v in os.environ.items() if not k.startswith("AWS_") and k not in ("KEEP", "DRY")}
        env |= {"DRY": "1", "PATH": f"{fake}:{os.environ['PATH']}", "TMPDIR": str(tmp_path), "ROLE_WAIT": "0"} | extra
        p = subprocess.run(["bash", str(RECIPE / script)], env=env, capture_output=True, text=True, timeout=60)
        assert "was called under DRY=1" not in p.stderr, p.stderr
        assert p.returncode == 0, p.stdout + p.stderr
        return p.stdout

    return _run


def commands(stdout: str) -> list[str]:
    return [line[2:] for line in stdout.splitlines() if line.startswith("$ ")]

"""A scheduled Lambda job, as ext-scheduled-merge and ext-maintenance deploy theirs: its role, its log group, the
function (timeout set, asynchronous retries 0), and a tagged schedule group with its schedules.

Every create carries the course tags (scripts/tags.py) and is written to the ledger with `record(...)`, so the
extension's clean_up.py deletes only what the ledger says it created. A resource that is there already is updated
in place (function code and settings, schedule) and keeps its ledger state.
"""
from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from tags import record, recorded, tags  # noqa: E402

REGION = "ca-central-1"
RUNTIME, ARCH = "python3.12", "arm64"
LAMBDA_TRUST = {"Version": "2012-10-17", "Statement": [
    {"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}]}
SCHEDULER_TRUST = {"Version": "2012-10-17", "Statement": [
    {"Effect": "Allow", "Principal": {"Service": "scheduler.amazonaws.com"}, "Action": "sts:AssumeRole"}]}


def zip_bytes(files: dict[str, Path], deps: list[str] | None = None) -> bytes:
    """A function package: `files` (name in the zip -> file) and, when `deps` is given, those packages installed
    for Lambda's arm64 Python 3.12 (`uv pip install --python-platform aarch64-manylinux2014`; needs the network)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, path in sorted(files.items()):
            z.write(path, name)
        if deps:
            with tempfile.TemporaryDirectory() as d:
                subprocess.run(["uv", "pip", "install", "--quiet", "--target", d, "--python-platform",
                                "aarch64-manylinux2014", "--python-version", "3.12", "--only-binary", ":all:",
                                *deps], check=True)
                for path in sorted(Path(d).rglob("*")):
                    if path.is_file() and "__pycache__" not in path.parts:
                        z.write(path, str(path.relative_to(d)))
    return buf.getvalue()


def ensure_role(iam, name: str, trust: dict, policy: dict, script: str, clean: str) -> str:
    """A role with one inline policy; created and recorded if missing, its policy replaced if it exists."""
    try:
        arn = iam.get_role(RoleName=name)["Role"]["Arn"]
        if recorded("iam-role", name) not in ("created", "borrowed"):
            record("iam-role", name, "borrowed", arn=arn, script=script)
    except ClientError as err:
        if err.response["Error"]["Code"] != "NoSuchEntity":
            raise
        arn = iam.create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps(trust), Tags=tags("iam"),
                              Description=f"{name}: scheduled job role (cdc-to-lake)")["Role"]["Arn"]
        record("iam-role", name, "created", arn=arn, script=script, clean=clean)
    iam.put_role_policy(RoleName=name, PolicyName=f"{name}-policy", PolicyDocument=json.dumps(policy))
    return arn


def ensure_log_group(logs, name: str, script: str, clean: str, days: int = 7) -> None:
    try:
        logs.create_log_group(logGroupName=name, tags=tags("logs"))
        record("log-group", name, "created", script=script, clean=clean, bills="per use")
    except ClientError as err:
        if err.response["Error"]["Code"] != "ResourceAlreadyExistsException":
            raise
    logs.put_retention_policy(logGroupName=name, retentionInDays=days)


def ensure_function(lam, name: str, role_arn: str, handler: str, code: bytes, env: dict, timeout: int,
                    memory: int, script: str, clean: str, sleep=time.sleep) -> str:
    """Create or update the function; asynchronous retries 0 (a scheduled job is retried by its next run)."""
    settings = dict(Role=role_arn, Handler=handler, Timeout=timeout, MemorySize=memory,
                    Environment={"Variables": env})
    try:
        arn = lam.get_function(FunctionName=name)["Configuration"]["FunctionArn"]
        lam.update_function_code(FunctionName=name, ZipFile=code, Architectures=[ARCH])
        lam.get_waiter("function_updated_v2").wait(FunctionName=name)
        lam.update_function_configuration(FunctionName=name, **settings)
        lam.get_waiter("function_updated_v2").wait(FunctionName=name)
    except ClientError as err:
        if err.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
        for attempt in range(10):          # a new role takes a few seconds before Lambda can assume it
            try:
                arn = lam.create_function(FunctionName=name, Runtime=RUNTIME, Architectures=[ARCH],
                                          Code={"ZipFile": code}, Tags=tags("lambda"), **settings)["FunctionArn"]
                break
            except ClientError as e:
                if e.response["Error"]["Code"] != "InvalidParameterValueException" or attempt == 9:
                    raise
                sleep(6)
        record("lambda-function", name, "created", arn=arn, script=script, clean=clean)
        lam.get_waiter("function_active_v2").wait(FunctionName=name)
    lam.put_function_event_invoke_config(FunctionName=name, MaximumRetryAttempts=0)
    return arn


def ensure_schedule_group(sch, name: str, script: str, clean: str) -> None:
    """Scheduler tags only schedule groups: every schedule of a job lives in its group, tagged at creation."""
    try:
        arn = sch.create_schedule_group(Name=name, Tags=tags("scheduler"))["ScheduleGroupArn"]
        record("schedule-group", name, "created", arn=arn, script=script, clean=clean)
    except ClientError as err:
        if err.response["Error"]["Code"] != "ConflictException":
            raise


def ensure_schedule(sch, definition: dict) -> str:
    try:
        sch.create_schedule(**definition)
        return "created"
    except ClientError as err:
        if err.response["Error"]["Code"] != "ConflictException":
            raise
        sch.update_schedule(**definition)
        return "updated"

"""Package and deploy the function `<PREFIX>-agent-sdk` behind a function URL with auth type AWS_IAM.

    uv run --env-file ext-sdk-lambda/.env python ext-sdk-lambda/deploy.py package   # build/agent-sdk.zip, arm64
    uv run --env-file ext-sdk-lambda/.env python ext-sdk-lambda/deploy.py up        # create or update, print the URL
    uv run --env-file ext-sdk-lambda/.env python ext-sdk-lambda/deploy.py url       # print the URL of a deployed one

`up` makes, in this order, each create with the course tags from scripts/tags.py and one ledger line
(state/ledger.jsonl): the lake bucket (scripts/lake.py up: created, or borrowed when it is there), the role
`<PREFIX>-agent-sdk-role` (write the function's logs, read its one parameter), the log group with 7 days'
retention, the SecureString `<PREFIX>-agent-sdk-token` with your course token, the package under `code/agent-sdk/`
of the bucket, the function (arm64, Python 3.12, 1,024 MB, 60 s) and its URL. Run again, it updates the code and
the settings of what is there. clean_up.py --yes deletes all of it but the bucket.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.append(str(HERE))

import lake  # noqa: E402
from settings import Settings, load_settings  # noqa: E402
from tags import record, tags  # noqa: E402

REGION = "ca-central-1"
BUILD = ROOT / "build"
ZIP = BUILD / "agent-sdk.zip"
CODE_PREFIX = "code/agent-sdk/"
# The function's own files: the package holds these, trips.parquet and the pinned libraries, nothing else of ours.
FUNCTION_FILES = ("handler.py", "settings.py", "engine.py", "sqlcheck.py", "toollog.py", "verdict.py", "refusal.py",
                  "trip_contracts.py", "caps.py", "agent_strands.py", "agent_pydantic.py")
REQUIREMENTS = ("strands-agents[openai]==1.64.0", "pydantic-ai-slim[openai]==2.71.0", "duckdb==2.0.0",
                "sqlglot==30.31.0", "pydantic-settings>=2.6")
LEFT_OUT = ("boto3", "botocore", "s3transfer")    # the Lambda Python runtime has them
MEMORY_MB, TIMEOUT_S, RETENTION_DAYS = 1024, 60, 7
SCRIPT, CLEAN = "ext-sdk-lambda/deploy.py up", "ext-sdk-lambda/clean_up.py --yes"
TRUST = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"},
                                                  "Action": "sts:AssumeRole"}]}


def role_policy(s: Settings, account: str) -> dict:
    """Least privilege: write its own log group, read its one parameter (SecureString under the AWS managed key)."""
    logs = f"arn:aws:logs:{REGION}:{account}:log-group:{s.log_group}"
    return {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"], "Resource": [logs, f"{logs}:*"]},
        {"Effect": "Allow", "Action": "ssm:GetParameter",
         "Resource": f"arn:aws:ssm:{REGION}:{account}:parameter/{s.token_param}"}]}


# ---- package --------------------------------------------------------------------------------------------------
def install(target: Path) -> None:
    """The pinned libraries for Python 3.12 on arm64 (manylinux wheels), without what the runtime has."""
    shutil.rmtree(target, ignore_errors=True)
    subprocess.run(["uv", "pip", "install", "--quiet", "--target", str(target), "--python-version", "3.12",
                    "--python-platform", "aarch64-manylinux_2_28", "--only-binary", ":all:", *REQUIREMENTS], check=True)
    for name in LEFT_OUT:
        for path in target.glob(f"{name}*"):
            shutil.rmtree(path) if path.is_dir() else path.unlink()


def build_zip(site: Path, trips: Path, out: Path = ZIP) -> dict:
    """The zip Lambda runs: our files and trips.parquet at the top, the libraries beside them. Returns its sizes."""
    if not trips.exists():
        raise SystemExit(f"{trips} is missing: run ext-sdk-lambda/make_trips.py first (README, Run it live)")
    out.parent.mkdir(parents=True, exist_ok=True)
    unzipped = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for name in FUNCTION_FILES:
            z.write(HERE / name, name)
            unzipped += (HERE / name).stat().st_size
        z.write(trips, "trips.parquet")
        unzipped += trips.stat().st_size
        for path in sorted(site.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                z.write(path, str(path.relative_to(site)))
                unzipped += path.stat().st_size
    return {"zipped": out.stat().st_size, "unzipped": unzipped}


# ---- up --------------------------------------------------------------------------------------------------------
def _exists(fn, gone: str, **kw):
    try:
        return fn(**kw)
    except ClientError as err:
        if err.response["Error"]["Code"] in (gone, "ResourceNotFoundException", "NoSuchEntity", "ParameterNotFound"):
            return None
        raise


def ensure_role(iam, s: Settings, account: str) -> str:
    found = _exists(iam.get_role, "NoSuchEntity", RoleName=s.role_name)
    if found:
        arn = found["Role"]["Arn"]
    else:
        arn = iam.create_role(RoleName=s.role_name, AssumeRolePolicyDocument=json.dumps(TRUST),
                              Description="ext-sdk-lambda: the agent function's role", Tags=tags("iam"))["Role"]["Arn"]
        record("iam-role", s.role_name, arn=arn, script=SCRIPT, clean=CLEAN, bills="monthly")
    iam.put_role_policy(RoleName=s.role_name, PolicyName="agent-sdk",
                        PolicyDocument=json.dumps(role_policy(s, account)))
    return arn


def ensure_log_group(logs, s: Settings) -> None:
    try:
        logs.create_log_group(logGroupName=s.log_group, tags=tags("logs"))
        record("log-group", s.log_group, script=SCRIPT, clean=CLEAN)
    except ClientError as err:
        if err.response["Error"]["Code"] != "ResourceAlreadyExistsException":
            raise
    logs.put_retention_policy(logGroupName=s.log_group, retentionInDays=RETENTION_DAYS)


def ensure_token(ssm, s: Settings) -> None:
    value = s.need("kb_mcp_token")
    if _exists(ssm.get_parameter, "ParameterNotFound", Name=s.token_param):
        ssm.put_parameter(Name=s.token_param, Value=value, Type="SecureString", Overwrite=True)
        return
    ssm.put_parameter(Name=s.token_param, Value=value, Type="SecureString", Tags=tags("ssm"),
                      Description="ext-sdk-lambda: the course token of the agent function")
    record("ssm-parameter", s.token_param, script=SCRIPT, clean=CLEAN)


def upload(s3, bucket: str, zip_path: Path) -> str:
    key = f"{CODE_PREFIX}package.zip"
    s3.upload_file(str(zip_path), bucket, key)
    record("s3-prefix", f"{bucket}/{CODE_PREFIX}", script=SCRIPT, clean=CLEAN)
    return key


def ensure_function(lam, s: Settings, role_arn: str, bucket: str, key: str) -> str:
    config = dict(Runtime="python3.12", Handler="handler.handler", Role=role_arn, MemorySize=MEMORY_MB,
                  Timeout=TIMEOUT_S, Environment={"Variables": s.deployed_env()},
                  LoggingConfig={"LogFormat": "Text", "LogGroup": s.log_group})
    found = _exists(lam.get_function, "ResourceNotFoundException", FunctionName=s.function_name)
    if found:
        lam.update_function_code(FunctionName=s.function_name, S3Bucket=bucket, S3Key=key, Architectures=["arm64"])
        _wait(lam, s.function_name)
        lam.update_function_configuration(FunctionName=s.function_name, **config)
        _wait(lam, s.function_name)
        return found["Configuration"]["FunctionArn"]
    for attempt in range(6):   # a new role takes a few seconds before Lambda can assume it
        try:
            arn = lam.create_function(FunctionName=s.function_name, Architectures=["arm64"],
                                      Code={"S3Bucket": bucket, "S3Key": key}, Tags=tags("lambda"),
                                      **config)["FunctionArn"]
            break
        except ClientError as err:
            if err.response["Error"]["Code"] != "InvalidParameterValueException" or attempt == 5:
                raise
            time.sleep(5)
    record("lambda-function", s.function_name, arn=arn, script=SCRIPT, clean=CLEAN)
    _wait(lam, s.function_name)
    return arn


def _wait(lam, name: str) -> None:
    for _ in range(60):
        c = lam.get_function_configuration(FunctionName=name)
        if c.get("State", "Active") == "Active" and c.get("LastUpdateStatus", "Successful") != "InProgress":
            return
        time.sleep(2)


def ensure_url(lam, s: Settings) -> str:
    found = _exists(lam.get_function_url_config, "ResourceNotFoundException", FunctionName=s.function_name)
    if found:
        return found["FunctionUrl"]
    url = lam.create_function_url_config(FunctionName=s.function_name, AuthType="AWS_IAM")["FunctionUrl"]
    record("lambda-function-url", s.function_name, script=SCRIPT, clean=CLEAN)
    return url


def up(s: Settings, zip_path: Path = ZIP, sess=None) -> str:
    sess = sess or boto3.session.Session(region_name=REGION)
    if not zip_path.exists():
        raise SystemExit(f"{zip_path} is missing: run `deploy.py package` first")
    lake.up(sess)
    account, bucket = lake.account_id(sess), lake.bucket_name(sess)
    role_arn = ensure_role(sess.client("iam"), s, account)
    ensure_log_group(sess.client("logs"), s)
    ensure_token(sess.client("ssm"), s)
    key = upload(sess.client("s3"), bucket, zip_path)
    lam = sess.client("lambda")
    ensure_function(lam, s, role_arn, bucket, key)
    return ensure_url(lam, s)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("step", choices=["package", "up", "url"])
    p.add_argument("--trips", type=Path, default=BUILD / "trips.parquet")
    a = p.parse_args(argv)
    if a.step == "package":
        site = BUILD / "agent-sdk-site"
        install(site)
        sizes = build_zip(site, a.trips)
        print(f"{ZIP.relative_to(ROOT)}: {sizes['zipped']:,} bytes zipped, {sizes['unzipped']:,} unzipped "
              f"(Lambda limits: 250 MB unzipped; over 50 MB zipped goes through S3)")
        return 0
    os.environ.setdefault("AWS_REGION", REGION)
    s = load_settings()
    if a.step == "url":
        print(boto3.client("lambda", region_name=REGION).get_function_url_config(
            FunctionName=s.function_name)["FunctionUrl"])
        return 0
    print(f"function URL (auth AWS_IAM): {up(s)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

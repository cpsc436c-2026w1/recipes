"""The deploy steps, one function each, in the order `deploy()` runs them. Every name comes from the settings.

    tables and books  ->  tools Lambda  ->  Gateway and its Lambda target  ->  secrets in Parameter Store
    ->  code zip in S3  ->  Runtime

Each step looks for what is already there by name, so a second deploy changes what differs and creates nothing twice.
A role is created only when its setting (LAMBDA_ROLE_ARN, GATEWAY_ROLE_ARN, RUNTIME_ROLE_ARN) is empty; each
created role is allowed only what this recipe does.
"""
from __future__ import annotations

import io
import json
import shutil
import subprocess
import time
import zipfile
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

from main.config import TARGET_NAME, Settings
from main.contracts import tool_schema
from main.store import read_books

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build"
CODE_KEY = "agent-runtime.zip"
# The entry file is not called main.py: inside the zip it sits beside the package main/, and the two names would clash.
ENTRY_POINT = "agent_entry.py"
WAIT_S = 5           # seconds between two looks at a resource that is being created. src: assumed
WAIT_TRIES = 60      # looks before a step gives up: 5 minutes. src: assumed
LAMBDA_TIMEOUT_S = 15    # the search is one Scan of a small table. src: assumed
LOG_DAYS = 7         # days the tools Lambda's log group keeps its events. src: assumed


@dataclass
class Clients:
    """The boto3 clients of one deploy or clean-up. Tests pass moto's and a stand-in for AgentCore."""
    dynamodb: object
    iam: object
    lambda_: object
    ssm: object
    s3: object
    logs: object
    sts: object
    agentcore: object

    @classmethod
    def for_region(cls, region: str) -> "Clients":
        make = lambda name: boto3.client(name, region_name=region)      # noqa: E731
        return cls(make("dynamodb"), make("iam"), make("lambda"), make("ssm"), make("s3"), make("logs"),
                   make("sts"), make("bedrock-agentcore-control"))


def account_id(c: Clients) -> str:
    return c.sts.get_caller_identity()["Account"]


def _code(err: ClientError) -> str:
    return err.response.get("Error", {}).get("Code", "")


def wait_for(look, done, failed=(), what: str = "", wait=time.sleep) -> str:
    """Call look() until it returns a status in `done`; stop on one in `failed` or after WAIT_TRIES looks."""
    status = ""
    for _ in range(WAIT_TRIES):
        status = look()
        if status in done:
            return status
        if status in failed:
            break
        wait(WAIT_S)
    raise RuntimeError(f"{what} is {status or 'not there'}, not {' or '.join(done)}")


# ---- 1. tables -------------------------------------------------------------------------------------------------
def ensure_tables(s: Settings, c: Clients, wait=time.sleep) -> None:
    """books (key: book_id) and holds (key: member, request_id), both billed per request."""
    wanted = {s.books_table: [("book_id", "HASH")], s.holds_table: [("member", "HASH"), ("request_id", "RANGE")]}
    for name, keys in wanted.items():
        try:
            c.dynamodb.create_table(
                TableName=name, BillingMode="PAY_PER_REQUEST",
                AttributeDefinitions=[{"AttributeName": k, "AttributeType": "S"} for k, _ in keys],
                KeySchema=[{"AttributeName": k, "KeyType": kind} for k, kind in keys])
        except ClientError as err:
            if _code(err) != "ResourceInUseException":
                raise
        wait_for(lambda: c.dynamodb.describe_table(TableName=name)["Table"]["TableStatus"], ("ACTIVE",),
                 what=f"table {name}", wait=wait)


def load_books(s: Settings, c: Clients) -> int:
    rows = read_books()
    for r in rows:
        item = {k: ({"N": str(Decimal(str(v)))} if isinstance(v, (int, float)) else {"S": str(v)})
                for k, v in r.items()}
        c.dynamodb.put_item(TableName=s.books_table, Item=item)
    return len(rows)


# ---- roles -----------------------------------------------------------------------------------------------------
def _trust(service: str, account: str, source_arn: str = "") -> dict:
    statement = {"Effect": "Allow", "Principal": {"Service": service}, "Action": "sts:AssumeRole"}
    if source_arn:      # only this account's AgentCore resources may assume the role
        statement["Condition"] = {"StringEquals": {"aws:SourceAccount": account},
                                  "ArnLike": {"aws:SourceArn": source_arn}}
    return {"Version": "2012-10-17", "Statement": [statement]}


def role_policies(s: Settings, account: str, gateway_arn: str = "") -> dict[str, dict]:
    """part -> (trust policy, permissions policy) of the three roles the recipe can create."""
    region, arn = s.aws_region, f"arn:aws:%s:{s.aws_region}:{account}:%s"
    function_arn = arn % ("lambda", f"function:{s.tools_function}")
    runtime_logs = arn % ("logs", "log-group:/aws/bedrock-agentcore/runtimes/*")
    allow = lambda actions, resources, **more: {"Effect": "Allow", "Action": actions, "Resource": resources, **more}  # noqa: E731
    lambda_policy = [
        allow(["logs:CreateLogStream", "logs:PutLogEvents"],
              arn % ("logs", f"log-group:/aws/lambda/{s.tools_function}:*")),
        allow(["dynamodb:Scan", "dynamodb:GetItem"], arn % ("dynamodb", f"table/{s.books_table}")),
        allow(["dynamodb:Query", "dynamodb:GetItem", "dynamodb:PutItem"],
              arn % ("dynamodb", f"table/{s.holds_table}"))]
    # The Runtime execution role of the AgentCore docs ("IAM Permissions for AgentCore Runtime", direct code
    # deployment), without the Bedrock model statements: this agent's model is behind the course LLM gateway.
    # src: receipt: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-permissions.html read 2026-11-16
    runtime_policy = [
        allow(["logs:DescribeLogStreams", "logs:CreateLogGroup"], runtime_logs),
        allow(["logs:DescribeLogGroups"], arn % ("logs", "log-group:*")),
        allow(["logs:CreateLogStream", "logs:PutLogEvents"], runtime_logs + ":log-stream:*"),
        allow(["xray:PutTraceSegments", "xray:PutTelemetryRecords", "xray:GetSamplingRules",
               "xray:GetSamplingTargets"], "*"),
        allow("cloudwatch:PutMetricData", "*",
              Condition={"StringEquals": {"cloudwatch:namespace": "bedrock-agentcore"}}),
        allow("ssm:GetParameter", [arn % ("ssm", f"parameter/{s.token_param}"),
                                   arn % ("ssm", f"parameter/{s.logfire_param}")]),
        allow("bedrock-agentcore:InvokeGateway",
              gateway_arn or arn % ("bedrock-agentcore", f"gateway/{s.gateway_name}-*"))]
    agentcore = "bedrock-agentcore.amazonaws.com"
    doc = lambda statements: {"Version": "2012-10-17", "Statement": statements}      # noqa: E731
    return {
        "lambda": (_trust("lambda.amazonaws.com", account), doc(lambda_policy)),
        "gateway": (_trust(agentcore, account, f"arn:aws:bedrock-agentcore:{region}:{account}:gateway/{s.gateway_name}-*"),
                    doc([allow("lambda:InvokeFunction", function_arn)])),
        "runtime": (_trust(agentcore, account, f"arn:aws:bedrock-agentcore:{region}:{account}:*"),
                    doc(runtime_policy)),
    }


def ensure_role(s: Settings, c: Clients, part: str, account: str, gateway_arn: str = "") -> str:
    """The ARN of the role for `part`: the one the setting names, left as it is, or one this recipe creates."""
    supplied = getattr(s, f"{part}_role_arn")
    if supplied:
        return supplied
    trust, policy = role_policies(s, account, gateway_arn)[part]
    name = s.role_name(part)
    try:
        arn = c.iam.create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps(trust),
                                Description=f"436C agent-agentcore recipe: {part}")["Role"]["Arn"]
    except ClientError as err:
        if _code(err) != "EntityAlreadyExists":
            raise
        arn = c.iam.get_role(RoleName=name)["Role"]["Arn"]
    c.iam.put_role_policy(RoleName=name, PolicyName="agent-agentcore", PolicyDocument=json.dumps(policy))
    return arn


def retry_new_role(call, wait=time.sleep):
    """A role made a moment ago is not yet known to every service: try again while the service refuses it."""
    for attempt in range(WAIT_TRIES):
        try:
            return call()
        except ClientError as err:
            new_role = _code(err) in ("InvalidParameterValueException", "ValidationException", "AccessDeniedException")
            if not new_role or "role" not in str(err).lower() or attempt == WAIT_TRIES - 1:
                raise
            wait(WAIT_S)


# ---- 2. the tools Lambda ---------------------------------------------------------------------------------------
def tools_zip() -> bytes:
    """handler.py, store.py and tools.py at the root of the zip: standard library and boto3 only."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        for source, name in ((ROOT / "tools_lambda" / "handler.py", "handler.py"),
                             (ROOT / "main" / "store.py", "store.py"), (ROOT / "main" / "tools.py", "tools.py")):
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))     # a fixed date: the same bytes each build
            info.external_attr = 0o644 << 16
            z.writestr(info, source.read_bytes(), zipfile.ZIP_DEFLATED)
    return buffer.getvalue()


def ensure_tools_lambda(s: Settings, c: Clients, role_arn: str, wait=time.sleep) -> str:
    name, code = s.tools_function, tools_zip()
    env = {"Variables": {"BOOKS_TABLE": s.books_table, "HOLDS_TABLE": s.holds_table}}
    try:
        c.logs.create_log_group(logGroupName=f"/aws/lambda/{name}")
    except ClientError as err:
        if _code(err) != "ResourceAlreadyExistsException":
            raise
    c.logs.put_retention_policy(logGroupName=f"/aws/lambda/{name}", retentionInDays=LOG_DAYS)
    try:
        arn = retry_new_role(lambda: c.lambda_.create_function(
            FunctionName=name, Runtime="python3.12", Architectures=["arm64"], Handler="handler.lambda_handler",
            Role=role_arn, Code={"ZipFile": code}, Timeout=LAMBDA_TIMEOUT_S, Environment=env), wait)["FunctionArn"]
    except ClientError as err:
        if _code(err) != "ResourceConflictException":
            raise
        arn = c.lambda_.update_function_code(FunctionName=name, ZipFile=code)["FunctionArn"]
        c.lambda_.get_waiter("function_updated_v2").wait(FunctionName=name)
        c.lambda_.update_function_configuration(FunctionName=name, Role=role_arn, Environment=env)
    c.lambda_.get_waiter("function_active_v2").wait(FunctionName=name)
    return arn


# ---- 3. the Gateway and its target -----------------------------------------------------------------------------
def find_gateway(s: Settings, c: Clients) -> str:
    """The id of this recipe's Gateway, or "" when there is none."""
    for page in c.agentcore.get_paginator("list_gateways").paginate():
        for g in page["items"]:
            if g["name"] == s.gateway_name:
                return g["gatewayId"]
    return ""


def find_target(c: Clients, gateway_id: str) -> str:
    for page in c.agentcore.get_paginator("list_gateway_targets").paginate(gatewayIdentifier=gateway_id):
        for t in page["items"]:
            if t["name"] == TARGET_NAME:
                return t["targetId"]
    return ""


def ensure_gateway(s: Settings, c: Clients, role_arn: str, wait=time.sleep) -> dict:
    """An MCP Gateway whose callers sign with AWS credentials (AWS_IAM). Returns its id, ARN and URL."""
    gateway_id = find_gateway(s, c)
    if not gateway_id:
        gateway_id = retry_new_role(lambda: c.agentcore.create_gateway(
            name=s.gateway_name, roleArn=role_arn, protocolType="MCP", authorizerType="AWS_IAM",
            description="436C agent-agentcore recipe: the example tools as MCP tools"), wait)["gatewayId"]
    wait_for(lambda: c.agentcore.get_gateway(gatewayIdentifier=gateway_id)["status"], ("READY",),
             ("FAILED", "UPDATE_UNSUCCESSFUL"), f"gateway {s.gateway_name}", wait)
    g = c.agentcore.get_gateway(gatewayIdentifier=gateway_id)
    return {"id": gateway_id, "arn": g["gatewayArn"], "url": g["gatewayUrl"]}


def ensure_target(c: Clients, gateway_id: str, function_arn: str, wait=time.sleep) -> str:
    """The Lambda target: the function's ARN and the tool schema built from the pydantic contracts."""
    config = {"mcp": {"lambda": {"lambdaArn": function_arn, "toolSchema": {"inlinePayload": tool_schema()}}}}
    creds = [{"credentialProviderType": "GATEWAY_IAM_ROLE"}]      # the Gateway invokes the function with its role
    target_id = find_target(c, gateway_id)
    if target_id:
        c.agentcore.update_gateway_target(gatewayIdentifier=gateway_id, targetId=target_id, name=TARGET_NAME,
                                          targetConfiguration=config, credentialProviderConfigurations=creds)
    else:
        target_id = c.agentcore.create_gateway_target(
            gatewayIdentifier=gateway_id, name=TARGET_NAME, targetConfiguration=config,
            credentialProviderConfigurations=creds,
            description=", ".join(t["name"] for t in tool_schema()))["targetId"]
    wait_for(lambda: c.agentcore.get_gateway_target(gatewayIdentifier=gateway_id, targetId=target_id)["status"],
             ("READY",), ("FAILED", "UPDATE_UNSUCCESSFUL"), f"gateway target {TARGET_NAME}", wait)
    return target_id


# ---- 4. secrets ------------------------------------------------------------------------------------------------
def put_secrets(s: Settings, c: Clients) -> list[str]:
    """The course token, and the Logfire token when there is one, as SecureString parameters. Values are not printed."""
    wanted = {s.token_param: s.need("kb_mcp_token")}
    if s.logfire_token:
        wanted[s.logfire_param] = s.logfire_token
    for name, value in wanted.items():
        c.ssm.put_parameter(Name=name, Value=value, Type="SecureString", Overwrite=True)
    return list(wanted)


# ---- 5. the code zip -------------------------------------------------------------------------------------------
def runtime_zip(with_dependencies: bool = True, build: Path = BUILD) -> Path:
    """build/agent-runtime.zip: runtime/agent_entry.py at the root, main/, data/, and (with_dependencies) the packages of
    runtime/requirements.txt as Linux arm64 wheels for Python 3.12, the platform the Runtime runs.
    src: receipt: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-get-started-code-deploy.html read 2026-11-16"""
    package = build / "runtime"
    shutil.rmtree(package, ignore_errors=True)
    package.mkdir(parents=True)
    if with_dependencies:
        subprocess.run(["uv", "pip", "install", "--quiet", "--python-platform", "aarch64-manylinux2014",
                        "--python-version", "3.12", "--target", str(package), "--only-binary=:all:",
                        "-r", str(ROOT / "runtime" / "requirements.txt")], check=True)
    shutil.copy(ROOT / "runtime" / ENTRY_POINT, package / ENTRY_POINT)
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", "infra.py", "run.py", "invoke.py")
    shutil.copytree(ROOT / "main", package / "main", ignore=ignore)
    shutil.copytree(ROOT / "data", package / "data")
    out = build / CODE_KEY
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(package.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                info = zipfile.ZipInfo(str(path.relative_to(package)), date_time=(2026, 1, 1, 0, 0, 0))
                info.external_attr = 0o644 << 16      # the Runtime reads POSIX permissions from the zip
                z.writestr(info, path.read_bytes(), zipfile.ZIP_DEFLATED)
    return out


def upload_code(s: Settings, c: Clients, account: str, path: Path) -> str:
    bucket = s.code_bucket(account)
    try:
        c.s3.create_bucket(Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": s.aws_region})
    except ClientError as err:
        if _code(err) != "BucketAlreadyOwnedByYou":
            raise
    c.s3.put_object(Bucket=bucket, Key=CODE_KEY, Body=path.read_bytes())
    return bucket


# ---- 6. the Runtime --------------------------------------------------------------------------------------------
def find_runtime(s: Settings, c: Clients) -> dict:
    """{"id", "arn"} of this recipe's Runtime, or {} when there is none."""
    for page in c.agentcore.get_paginator("list_agent_runtimes").paginate():
        for r in page["agentRuntimes"]:
            if r["agentRuntimeName"] == s.runtime_name:
                return {"id": r["agentRuntimeId"], "arn": r["agentRuntimeArn"]}
    return {}


def ensure_runtime(s: Settings, c: Clients, role_arn: str, bucket: str, gateway_url: str, wait=time.sleep) -> dict:
    """The Runtime from the zip in S3. Its environment variables are the non-secret settings (Settings.deployed_env)."""
    args = dict(
        agentRuntimeArtifact={"codeConfiguration": {"code": {"s3": {"bucket": bucket, "prefix": CODE_KEY}},
                                                    "runtime": "PYTHON_3_12", "entryPoint": [ENTRY_POINT]}},
        roleArn=role_arn, networkConfiguration={"networkMode": "PUBLIC"},
        lifecycleConfiguration={"idleRuntimeSessionTimeout": s.runtime_idle_timeout_s,
                                "maxLifetime": s.runtime_max_lifetime_s},
        environmentVariables=s.deployed_env(gateway_url),
        description="436C agent-agentcore recipe: a Strands agent, tools behind the Gateway")
    found = find_runtime(s, c)
    if found:
        c.agentcore.update_agent_runtime(agentRuntimeId=found["id"], **args)
    else:
        made = retry_new_role(lambda: c.agentcore.create_agent_runtime(agentRuntimeName=s.runtime_name, **args), wait)
        found = {"id": made["agentRuntimeId"], "arn": made["agentRuntimeArn"]}
    wait_for(lambda: c.agentcore.get_agent_runtime(agentRuntimeId=found["id"])["status"], ("READY",),
             ("CREATE_FAILED", "UPDATE_FAILED"), f"runtime {s.runtime_name}", wait)
    return found


def deploy(s: Settings, c: Clients, log=print, wait=time.sleep, with_dependencies: bool = True) -> dict:
    s.need("kb_mcp_token")           # asked for before the first AWS call: the Runtime cannot run without them
    s.need("course_gateway_url")
    account = account_id(c)
    ensure_tables(s, c, wait)
    log(f"tables {s.books_table} and {s.holds_table}: {load_books(s, c)} books")
    function_arn = ensure_tools_lambda(s, c, ensure_role(s, c, "lambda", account), wait)
    log(f"function {s.tools_function}")
    gateway = ensure_gateway(s, c, ensure_role(s, c, "gateway", account), wait)
    ensure_target(c, gateway["id"], function_arn, wait)
    log(f"gateway {s.gateway_name} with target {TARGET_NAME}: {len(tool_schema())} tools")
    log(f"parameters {', '.join(put_secrets(s, c))} (SecureString)")
    bucket = upload_code(s, c, account, runtime_zip(with_dependencies))
    log(f"code s3://{bucket}/{CODE_KEY}")
    runtime = ensure_runtime(s, c, ensure_role(s, c, "runtime", account, gateway["arn"]), bucket, gateway["url"], wait)
    log(f"runtime {s.runtime_name}: READY")
    return {"gateway": gateway, "runtime": runtime, "function_arn": function_arn, "bucket": bucket}

"""behave set-up for the scenarios in this folder. Run them from the recipe folder: `uv run behave`.

Every scenario runs offline, in one process: the gateway's own code (ext-deploy/src/gateway) as a real server on a
free port of 127.0.0.1, a fake upstream (ext-deploy/tests/fake_upstream.py) as a second server, and moto in place of
DynamoDB, SNS, Secrets Manager and Lambda. The client of the main scenario (main/call_gateway.py) calls that gateway
over HTTP. No AWS account, no course token, no provider key; the credentials are made up.
"""

from __future__ import annotations

import io
import logging
import os
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for folder in (ROOT / "features", ROOT / "ext-deploy" / "tests"):
    sys.path.insert(0, str(folder))

NOT_SET = ("AWS_PROFILE", "GATEWAY_URL", "COURSE_MCP_TOKEN", "COURSE_GATEWAY_URL", "KB_MCP_TOKEN", "CHAT_MODEL")
OWNERS: list[int] = []         # one more per scenario, for the whole run


class LogLines(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.INFO)
        self.lines: list[str] = []

    def emit(self, record):
        self.lines.append(f"{record.levelname} {record.getMessage()}")


def before_all(context):
    for name in NOT_SET:
        os.environ.pop(name, None)
    import conftest as harness            # ext-deploy's test harness: sets made-up AWS settings when it is imported
    import boto3
    from moto import mock_aws

    context.harness = harness
    context.moto = mock_aws()
    context.moto.start()
    context.topic_arn = harness.create_aws_resources()
    os.environ["TOPIC_ARN"] = context.topic_arn
    # the function that `gw-admin kill` and `open` change, as the template names it
    role = boto3.client("iam").create_role(RoleName="llm-gateway-fn", AssumeRolePolicyDocument="{}")["Role"]["Arn"]
    code = io.BytesIO()
    zipfile.ZipFile(code, "w").writestr("run.sh", "#!/bin/sh\n")
    boto3.client("lambda", region_name="ca-central-1").create_function(
        FunctionName="llm-gateway", Runtime="python3.13", Role=role, Handler="run.sh", Code={"ZipFile": code.getvalue()})
    from gateway import aws, secrets

    aws.reset()
    secrets.reset_for_tests()
    context.up_server, context.up_thread, context.upstream_url = harness.start_server(harness.fake_upstream.app)
    os.environ["UPSTREAM_BASE_URL"] = context.upstream_url + "/v1"
    from gateway.app import app

    context.gw_server, context.gw_thread, context.gateway_url = harness.start_server(app)


def before_scenario(context, scenario):
    for name in NOT_SET:
        os.environ.pop(name, None)
    context.harness.fake_upstream.received.clear()
    OWNERS.append(len(OWNERS) + 1)
    context.owner = f"owner{OWNERS[-1]}"            # a new owner per scenario: its notices are its own
    context.key_id = context.key = None
    context.response = None
    context.printed = ""
    context.printed_err = ""
    context.exit_code = None
    context.slept = []
    context.log = LogLines()
    logging.getLogger("gateway").addHandler(context.log)


def after_scenario(context, scenario):
    logging.getLogger("gateway").removeHandler(context.log)


def after_all(context):
    context.gw_server.should_exit = context.up_server.should_exit = True
    context.gw_thread.join(5)
    context.up_thread.join(5)
    context.moto.stop()

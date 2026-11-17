"""behave set-up for the scenarios in this folder. Run them from the recipe folder: `uv run behave`.

Every scenario runs offline, with made-up AWS credentials and no course token, in a temporary folder of its own.
What stands in for AWS (features/support.py):
- moto: DynamoDB and its stream, SQS, IAM, STS, CloudWatch Logs, Lambda's control plane (functions and event source
  mappings as records; moto never runs a function here);
- OpenSearch Serverless's control plane (moto has no collection groups and no access policies), Lambda's Invoke and
  a refusal a scenario names: answered where boto3 hands a call to the network;
- the collection's endpoint: answered where `requests` (the scripts) or `urllib` (the function) sends a request;
- Lambda's event source mapping on the offline path: the recipe's own stand-in, main/standin.py, which the offline
  run uses. What it models and what it does not is in the README, "Run it".
No scenario starts a container: moto's hook that would run the function in Docker for each stream record is
replaced by one that fails the scenario.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for folder in (ROOT / "features", ROOT / "ext-embeddings", ROOT / "ext-zero-etl", ROOT):
    sys.path.insert(0, str(folder))

OFFLINE = {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_SESSION_TOKEN": "testing",
           "AWS_DEFAULT_REGION": "ca-central-1", "AWS_REGION": "ca-central-1", "AWS_EC2_METADATA_DISABLED": "true",
           "PREFIX": "recipe-ana"}
NOT_SET = ("AWS_PROFILE", "AWS_DEFAULT_PROFILE", "GATEWAY_URL", "COURSE_MCP_TOKEN", "COURSE_GATEWAY_URL", "KB_MCP_TOKEN", "LAMBDA_ROLE_ARN",
           "PIPELINE_ROLE_ARN", "BUCKET", "INDEX_VERSION")


def before_all(context):
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("opensearch").setLevel(logging.ERROR)
    from moto.awslambda.models import LambdaBackend, LambdaFunction

    def no_docker(*a, **kw):
        raise AssertionError("moto was about to run a Lambda function in a container")

    LambdaBackend.send_dynamodb_items = lambda self, *a, **kw: None   # the offline path delivers records itself
    LambdaFunction._invoke_lambda = no_docker


def before_scenario(context, scenario):
    os.environ.update(OFFLINE)
    for name in NOT_SET:
        os.environ.pop(name, None)
    context.tmp = Path(tempfile.mkdtemp(prefix="zero-etl-search-scenario-"))
    context.printed, context.exit_code, context.error = "", None, None
    context.sleeps = []


def after_scenario(context, scenario):
    shutil.rmtree(context.tmp, ignore_errors=True)

"""behave set-up for the scenarios in this folder. Run them from the recipe folder: `uv run behave`.

Every scenario runs offline, inside moto (a local stand-in for AWS), with made-up credentials and in a temporary
folder. A run with `--target aws` is called inside moto too, so its boto3 calls never leave the process. What moto
does not play (Athena's answers, a Firehose stream that rejects records, a refused call, a resource that is still
being deleted) is put in at the one place where boto3 hands a call to the network (features/support.py, Calls).
The lake of the offline runs is the recipe's own: a real Iceberg table on disk and DuckDB (main/lake.py).
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for folder in (ROOT / "features", ROOT / "tests", ROOT / "ext-lag-alarm", ROOT / "ext-rollback-or-schema",
               ROOT / "ext-maintenance", ROOT):
    sys.path.insert(0, str(folder))

OFFLINE = {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_SESSION_TOKEN": "testing",
           "AWS_DEFAULT_REGION": "ca-central-1", "AWS_EC2_METADATA_DISABLED": "true", "PREFIX": "recipe-ana"}
NOT_SET = ("AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_REGION", "GATEWAY_URL", "COURSE_MCP_TOKEN", "COURSE_GATEWAY_URL", "KB_MCP_TOKEN", "BUCKET",
           "LAMBDA_ROLE_ARN", "FIREHOSE_ROLE_ARN", "SCHEDULER_ROLE_ARN", "BUFFER_SECONDS", "LAG_LIMIT_SECONDS",
           "ATHENA_WORKGROUP", "DELIVERY_STREAM")


def before_all(context):
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("pyiceberg").setLevel(logging.ERROR)
    # When a table with an event source mapping gets a write, moto starts the Lambda function in a Docker
    # container (and its use_docker setting does not cover that path). The scenarios must not start containers:
    # here moto answers the invocation itself with an empty reply, so on the `--target aws` path NO function runs:
    # the changes of such a run do not go through main/handler.py, and what the run prints comes from the Athena
    # answers a scenario states. The function is exercised directly, in features/function.feature.
    from moto.awslambda import models as moto_lambda

    moto_lambda.LambdaFunction._invoke_lambda = lambda self, event=None: ("{}", False, "")


def before_scenario(context, scenario):
    import support
    from moto import mock_aws

    os.environ.update(OFFLINE)
    for name in NOT_SET:
        os.environ.pop(name, None)
    context.tmp = Path(tempfile.mkdtemp(prefix="cdc-to-lake-scenario-"))
    context.moto = mock_aws()
    context.moto.start()
    context.calls = support.Calls()
    context.calls.start()
    context.printed, context.exit_code, context.error = "", None, None
    context.sleeps = []          # the waits of a run with --target aws: written down, not waited (support.no_waits)


def after_scenario(context, scenario):
    context.calls.stop()
    context.moto.stop()
    shutil.rmtree(context.tmp, ignore_errors=True)

"""behave set-up for the scenarios in this folder. Run them from the recipe folder: `uv run behave`.

Every scenario runs offline, with made-up credentials and in temporary folders:
- the handler (main/handler.py) is loaded inside moto, a local stand-in for AWS, and called the way a function URL
  calls it;
- a script runs dry (DRY=1, with an `aws` and a `curl` that fail the scenario when called) or for real against a
  stand-in `aws` that logs each call and answers what the scenario tells it to (tests/harness.py);
- a feature tagged @moto_server runs the dynamodb recipe's scripts and this recipe's handler against a moto server
  on localhost, with the real AWS CLI; it is skipped when the AWS CLI is not installed.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for folder in (ROOT / "features", ROOT / "tests"):
    sys.path.insert(0, str(folder))

OFFLINE = {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_SESSION_TOKEN": "testing",
           "AWS_DEFAULT_REGION": "ca-central-1", "AWS_REGION": "ca-central-1", "AWS_EC2_METADATA_DISABLED": "true"}
NOT_SET = ("AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_ENDPOINT_URL", "GATEWAY_URL", "COURSE_MCP_TOKEN", "COURSE_GATEWAY_URL", "KB_MCP_TOKEN", "KEEP", "DRY",
           "ROLE_ARN", "PREFIX")


def _offline():
    os.environ.update(OFFLINE)
    for name in NOT_SET:
        os.environ.pop(name, None)


def before_all(context):
    _offline()


def before_feature(context, feature):
    context.moto_server = None
    if "moto_server" in feature.tags:
        if not shutil.which("aws"):
            feature.skip("the AWS CLI is not installed; the other features still run")
            return
        from moto.server import ThreadedMotoServer

        context.moto_server = ThreadedMotoServer(ip_address="127.0.0.1", port=0)
        context.moto_server.start()
        host, port = context.moto_server.get_host_and_port()
        context.moto_url = f"http://{host}:{port}"


def after_feature(context, feature):
    if context.moto_server:
        context.moto_server.stop()


def before_scenario(context, scenario):
    _offline()
    context.tmp = Path(tempfile.mkdtemp(prefix="lambda-dynamodb-scenario-"))
    context.env = {}             # extra environment for the script runs of this scenario
    context.run = None           # the last script run: support.Run
    context.moto = None          # moto inside this process, for the handler scenarios
    context.handler = None


def after_scenario(context, scenario):
    if context.moto is not None:
        context.moto.stop()
    shutil.rmtree(context.tmp, ignore_errors=True)

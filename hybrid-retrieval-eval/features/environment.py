"""behave set-up for the scenarios in this folder. Run them from the recipe folder: `uv run behave`.

Every scenario runs offline, with made-up AWS credentials, no course token and no gateway address, in a temporary
folder of its own. What stands in for the services (features/support.py):
- the course LLM gateway: the recipe's own httpx clients, with their transport replaced by one that answers inside
  the process (httpx.MockTransport);
- OpenSearch Serverless: its control-plane calls are answered where boto3 hands a call to the network (moto does
  not have collection groups or access policies); STS and IAM go on to moto;
- the collection's endpoint: opensearch-py's connection, answered inside the process.
Nothing opens a network connection, and no scenario starts a container.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for folder in (ROOT / "features", ROOT / "ext-filters", ROOT / "ext-model-routing", ROOT):
    sys.path.insert(0, str(folder))

OFFLINE = {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_SESSION_TOKEN": "testing",
           "AWS_DEFAULT_REGION": "ca-central-1", "AWS_EC2_METADATA_DISABLED": "true", "PREFIX": "recipe-ana"}
NOT_SET = ("AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_REGION", "GATEWAY_URL", "COURSE_MCP_TOKEN", "COURSE_GATEWAY_URL", "KB_MCP_TOKEN")


def before_all(context):
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("opensearch").setLevel(logging.ERROR)


def before_scenario(context, scenario):
    os.environ.update(OFFLINE)
    for name in NOT_SET:
        os.environ.pop(name, None)
    context.tmp = Path(tempfile.mkdtemp(prefix="hybrid-retrieval-eval-scenario-"))
    context.printed, context.exit_code, context.error = "", None, None
    context.requests = []       # every request the gateway stand-in received
    context.sleeps = []         # every wait the recipe asked for: written down, not waited


def after_scenario(context, scenario):
    shutil.rmtree(context.tmp, ignore_errors=True)

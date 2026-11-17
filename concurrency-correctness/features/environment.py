"""behave set-up for the scenarios in this folder. Run them from the recipe folder: `uv run behave`.

Every scenario runs offline, inside moto (a local stand-in for AWS), with made-up credentials. A run with
`--target aws` is called inside moto too, so its boto3 calls never leave the process. What moto does not play (a
refused call, a table that is still being deleted, a reply that never comes) is put in at the one place where boto3
hands a call to the network (features/support.py, Calls).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for folder in (ROOT / "features", ROOT / "tests", ROOT / "ext-idempotent-retry", ROOT / "main", ROOT):
    sys.path.insert(0, str(folder))

OFFLINE = {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_SESSION_TOKEN": "testing",
           "AWS_DEFAULT_REGION": "ca-central-1", "AWS_EC2_METADATA_DISABLED": "true"}
NOT_SET = ("AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_REGION", "GATEWAY_URL", "COURSE_MCP_TOKEN", "COURSE_GATEWAY_URL", "KB_MCP_TOKEN", "PREFIX")


def before_all(context):
    import logging

    logging.getLogger("botocore").setLevel(logging.WARNING)     # one line per client is noise in the report


def before_scenario(context, scenario):
    import support
    from moto import mock_aws

    os.environ.update(OFFLINE)
    for name in NOT_SET:
        os.environ.pop(name, None)
    context.moto = mock_aws()
    context.moto.start()
    context.calls = support.Calls()          # every boto3 call of the scenario, and the answers put in by hand
    context.calls.start()
    context.printed = ""
    context.exit_code = None
    context.error = None
    context.sleeps = []


def after_scenario(context, scenario):
    context.calls.stop()
    context.moto.stop()

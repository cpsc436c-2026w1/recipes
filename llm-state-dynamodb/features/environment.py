"""behave set-up for the scenarios in this folder. Run them from the recipe folder: `uv run behave`.

Every scenario runs offline: made-up AWS credentials, PREFIX "test", no AWS_PROFILE. Unless a scenario is tagged
@own_moto (a run with `--target local`, which starts moto itself), it runs inside moto, a local stand-in for
DynamoDB, so its boto3 calls never leave the process. Nothing is written into the recipe folder.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for folder in (ROOT / "features", ROOT, ROOT / "ext-traces", ROOT / "ext-answer-cache", ROOT / "main"):
    sys.path.insert(0, str(folder))

OFFLINE = {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_SESSION_TOKEN": "testing",
           "AWS_DEFAULT_REGION": "ca-central-1", "AWS_REGION": "ca-central-1", "AWS_EC2_METADATA_DISABLED": "true",
           "PREFIX": "test"}
NOT_SET = ("AWS_PROFILE", "GATEWAY_URL", "COURSE_MCP_TOKEN", "COURSE_GATEWAY_URL", "KB_MCP_TOKEN")


def before_scenario(context, scenario):
    from support import Calls, Clock

    os.environ.update(OFFLINE)
    for name in NOT_SET:
        os.environ.pop(name, None)
    context.clock = Clock()
    context.printed = ""
    context.printed_err = ""
    context.exit_code = None
    context.error = None
    context.undo = []            # functions that put back what a scenario patched
    context.calls = Calls()      # every AWS call the recipe made, and the answers a scenario chose for some
    context.calls.install(context)
    context.moto = None
    from support import refuse_network

    refuse_network(context)  # every scenario: nothing may leave the process, whoever starts moto
    if "own_moto" not in scenario.effective_tags:
        from moto import mock_aws

        context.moto = mock_aws()
        context.moto.start()


def after_scenario(context, scenario):
    for undo in reversed(context.undo):
        undo()
    if context.moto is not None:
        context.moto.stop()

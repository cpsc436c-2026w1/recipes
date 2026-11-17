"""behave set-up for the scenarios in this folder. Run them from the recipe folder: `uv run behave`.

Every scenario runs offline: made-up AWS credentials, no course token, no gateway address. A scenario tagged @aws
(or in a feature tagged @aws) runs inside moto, a local stand-in for AWS, so its boto3 calls never leave the process.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for folder in (ROOT / "features", ROOT / "tests", ROOT / "ext-shiny-app", ROOT / "ext-feedback",
               ROOT / "ext-trace-alarms", ROOT / "ext-guardrails-redteam", ROOT / "ext-answer-cache", ROOT):
    sys.path.insert(0, str(folder))

OFFLINE = {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_SESSION_TOKEN": "testing",
           "AWS_DEFAULT_REGION": "ca-central-1", "AWS_REGION": "ca-central-1", "AWS_EC2_METADATA_DISABLED": "true",
           "PREFIX": "test"}
NOT_SET = ("AWS_PROFILE", "GATEWAY_URL", "COURSE_MCP_TOKEN", "COURSE_GATEWAY_URL", "KB_MCP_TOKEN", "CHAT_MODEL")


def before_all(context):
    os.environ.update(OFFLINE)
    for name in NOT_SET:
        os.environ.pop(name, None)
    from main.data import data_version, make_data

    context.data_dir = Path(tempfile.mkdtemp(prefix="dashboard-chat-data-"))
    context.paths = make_data(context.data_dir)       # the same 1,200 orders and 4 stores on every machine
    context.version = data_version(context.paths)


def before_scenario(context, scenario):
    os.environ.update(OFFLINE)
    for name in NOT_SET:
        os.environ.pop(name, None)
    context.tmp = Path(tempfile.mkdtemp(prefix="dashboard-chat-scenario-"))
    context.traces = None        # where trace items go; None: nowhere
    context.printed = ""         # everything the recipe printed in this scenario
    context.printed_err = ""
    context.exit_code = None
    context.results = []         # results of questions asked through the answer cache, in order
    context.moto = None
    if "aws" in scenario.effective_tags:
        from moto import mock_aws

        context.moto = mock_aws()
        context.moto.start()


def after_scenario(context, scenario):
    if context.moto is not None:
        context.moto.stop()
    shutil.rmtree(context.tmp, ignore_errors=True)


def after_all(context):
    shutil.rmtree(context.data_dir, ignore_errors=True)

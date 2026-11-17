"""behave set-up for the scenarios in this folder. Run them from the recipe folder: `uv run behave`.

Every scenario runs offline: the settings are the lines of .env.example, the AWS credentials are made up, the model
is the stub, moto stands in for the AWS services it has and tests/fakes.py for AgentCore.
"""
from __future__ import annotations

import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.offline_env import example_env  # noqa: E402


def before_all(context):
    logging.getLogger("strands").setLevel(logging.ERROR)
    context.saved_env = dict(os.environ)


def before_scenario(context, scenario):
    example_env()
    context.tmp = Path(tempfile.mkdtemp(prefix="agent-agentcore-"))
    context.behaviour = "careful"
    context.request_id = None
    context.late = False
    context.closers = []          # functions that end what a scenario started (moto, a local server)
    context.library = None
    context.stub = None
    context.report = None
    context.result = None
    context.error = None
    context.aws = None


def after_scenario(context, scenario):
    for close in reversed(context.closers):
        close()
    shutil.rmtree(context.tmp, ignore_errors=True)


def after_all(context):
    os.environ.clear()
    os.environ.update(context.saved_env)

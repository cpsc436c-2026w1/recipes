"""behave set-up for the scenarios in this folder. Run them from the recipe folder: `uv run behave`.

Every scenario runs offline: the two models are the recipe's own stub (main/stub.py), or, for a gateway run, a
server on 127.0.0.1 that answers as the stub does (features/support.py). No course token, no AWS account. Every run
writes to a temporary folder, never to runs/ or output/ of the recipe.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for folder in (ROOT / "features", ROOT / "features" / "steps", ROOT):
    sys.path.insert(0, str(folder))

OFFLINE = {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_SESSION_TOKEN": "testing",
           "AWS_DEFAULT_REGION": "ca-central-1", "AWS_EC2_METADATA_DISABLED": "true"}
NOT_SET = ("AWS_PROFILE", "GATEWAY_URL", "COURSE_MCP_TOKEN", "COURSE_GATEWAY_URL", "KB_MCP_TOKEN")


def before_all(context):
    context.shared = Path(tempfile.mkdtemp(prefix="prompt-optimization-shared-"))
    context.runs = {}            # one finished run per command line, reused by the scenarios that only read it


def before_scenario(context, scenario):
    os.environ.update(OFFLINE)
    for name in NOT_SET:
        os.environ.pop(name, None)
    context.tmp = Path(tempfile.mkdtemp(prefix="prompt-optimization-scenario-"))
    context.printed = ""
    context.printed_err = ""
    context.exit_code = None
    context.out = None           # the folder the last run wrote to
    context.gateway = None
    context.calls = []           # (kind, model, system, user) of every stub call, when a scenario records them


def after_scenario(context, scenario):
    if context.gateway is not None:
        context.gateway.close()
    shutil.rmtree(context.tmp, ignore_errors=True)


def after_all(context):
    shutil.rmtree(context.shared, ignore_errors=True)

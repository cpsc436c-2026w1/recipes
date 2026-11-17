"""behave set-up for the scenarios in this folder. Run them from the recipe folder: `uv run behave`.

Every scenario runs offline: no course token, no gateway address, made-up AWS credentials (the recipe never calls
AWS; the credentials are there so that a call by mistake could not reach a real account). Each scenario gets a
temporary folder, and nothing is written into the recipe folder.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for folder in (ROOT / "features", ROOT / "ext-eval", ROOT):
    sys.path.insert(0, str(folder))

OFFLINE = {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_SESSION_TOKEN": "testing",
           "AWS_DEFAULT_REGION": "ca-central-1", "AWS_EC2_METADATA_DISABLED": "true"}
NOT_SET = ("AWS_PROFILE", "GATEWAY_URL", "COURSE_MCP_TOKEN", "COURSE_GATEWAY_URL", "KB_MCP_TOKEN", "SMALL_MODEL", "LARGE_MODEL")


def before_scenario(context, scenario):
    os.environ.update(OFFLINE)
    for name in NOT_SET:
        os.environ.pop(name, None)
    context.tmp = Path(tempfile.mkdtemp(prefix="model-routing-scenario-"))
    context.log_path = context.tmp / "misroutes.jsonl"
    context.routes_path = context.tmp / "routes.jsonl"
    context.printed = ""
    context.printed_err = ""
    context.exit_code = None
    context.error = None
    context.requests = []        # every HTTP request a gateway stand-in received
    context.out = None           # the result of the last request handled


def after_scenario(context, scenario):
    shutil.rmtree(context.tmp, ignore_errors=True)

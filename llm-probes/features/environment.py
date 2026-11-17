"""behave set-up for the scenarios in this folder. Run them from the recipe folder: `uv run behave`.

Every scenario runs offline: the endpoint is tests/fake_endpoint.py behind httpx.MockTransport, so the recipe's
own requests are built and read as in a real run and never leave the process. No key of a real endpoint, no AWS
account. A scenario's files are in a temporary folder.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for folder in (ROOT / "features", ROOT / "tests", ROOT / "main"):
    sys.path.insert(0, str(folder))

OFFLINE = {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_SESSION_TOKEN": "testing",
           "AWS_DEFAULT_REGION": "ca-central-1", "AWS_EC2_METADATA_DISABLED": "true"}
NOT_SET = ("AWS_PROFILE", "GATEWAY_URL", "COURSE_MCP_TOKEN", "COURSE_GATEWAY_URL", "KB_MCP_TOKEN", "LLM_BASE_URL", "LLM_API_KEY", "OPENAI_BASE_URL",
           "OPENAI_API_KEY")


def before_scenario(context, scenario):
    from support import Endpoint

    os.environ.update(OFFLINE)
    for name in NOT_SET:
        os.environ.pop(name, None)
    context.tmp = Path(tempfile.mkdtemp(prefix="llm-probes-scenario-"))
    context.endpoint = Endpoint()        # the stand-in; a Given step changes how it answers
    context.printed = ""
    context.printed_err = ""
    context.exit_code = None


def after_scenario(context, scenario):
    shutil.rmtree(context.tmp, ignore_errors=True)

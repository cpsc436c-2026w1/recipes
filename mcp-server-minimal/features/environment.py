"""behave set-up for the scenarios in this folder. Run them from the recipe folder: `uv run behave`.

Every scenario runs offline and on this machine only: the recipe's own server is started on a free port of
127.0.0.1 and called with the recipe's own client code, so nothing stands in for anything. The sample tables and
every file a scenario writes are in temporary folders; nothing is written into the recipe folder. The AWS
credentials are made up (the recipe never calls AWS; a call by mistake could not reach a real account).
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for folder in (ROOT / "features", ROOT / "features" / "steps", ROOT / "ext-confirmed-write", ROOT):
    sys.path.insert(0, str(folder))

OFFLINE = {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_SESSION_TOKEN": "testing",
           "AWS_DEFAULT_REGION": "ca-central-1", "AWS_EC2_METADATA_DISABLED": "true"}
NOT_SET = ("AWS_PROFILE", "GATEWAY_URL", "COURSE_MCP_TOKEN", "COURSE_GATEWAY_URL", "KB_MCP_TOKEN", "MCP_SERVER_TOKEN", "APPROVAL_SECRET", "APPROVER",
           "MCP_HOST", "MCP_PORT", "DATA_DIR", "MAX_ROWS", "REFUNDS_DB", "MCP_ALLOWED_ORIGINS")


def before_all(context):
    from main.data import ensure_data

    context.data_dir = Path(tempfile.mkdtemp(prefix="mcp-server-minimal-data-"))
    context.tables = ensure_data(context.data_dir)       # the same 6 stores and 200 orders on every machine


def before_scenario(context, scenario):
    from support import LogLines

    os.environ.update(OFFLINE)
    for name in NOT_SET:
        os.environ.pop(name, None)
    context.tmp = Path(tempfile.mkdtemp(prefix="mcp-server-minimal-scenario-"))
    context.printed = ""
    context.printed_err = ""
    context.exit_code = None
    context.servers = []          # (uvicorn server, thread) started in this scenario
    context.engine = None
    context.store = None
    context.url = None
    context.result = None         # the last tool result
    context.log_lines = LogLines()
    logger = logging.getLogger("mcp-server-minimal")
    logger.setLevel(logging.INFO)
    logger.addHandler(context.log_lines)


def after_scenario(context, scenario):
    for server, thread in context.servers:
        server.should_exit = True
        thread.join(timeout=5)
    if context.store is not None:
        context.store.close()
    if context.engine is not None:
        context.engine.close()
    logging.getLogger("mcp-server-minimal").removeHandler(context.log_lines)
    shutil.rmtree(context.tmp, ignore_errors=True)


def after_all(context):
    shutil.rmtree(context.data_dir, ignore_errors=True)

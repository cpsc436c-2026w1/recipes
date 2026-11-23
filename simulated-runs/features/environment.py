"""behave set-up: run the scenarios from the recipe folder with `uv run behave`.

Offline (every scenario not tagged @live): moto stands in for AWS, with made-up credentials and no profile, so no
call leaves the process; PREFIX is `recipe`, the names the scenarios state. The data sets are generated once into a
temporary folder. A scenario tagged @live runs against the account of your AWS_PROFILE, with your PREFIX, and is
left out of a plain `uv run behave` by behave.ini (`default_tags = not (@live or @eval)`).
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "sim"), str(ROOT / "scripts"), str(ROOT)]

OFFLINE = {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_SESSION_TOKEN": "testing",
           "AWS_DEFAULT_REGION": "ca-central-1", "AWS_EC2_METADATA_DISABLED": "true", "PREFIX": "recipe",
           "TEAM": "recipe", "OWNER": "tester"}
NOT_SET = ("AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_REGION", "PURPOSE", "PIPELINE_TABLE", "CHECK_TZ", "CHECK_CRON",
           "CHECK_DELAY_MIN", "CHECK_TARGET", "CHECK_QUERY", "CHECK_EXPECTED", "LAMBDA_ROLE_ARN",
           "SCHEDULER_ROLE_ARN", "KB_MCP_TOKEN", "COURSE_MCP_TOKEN", "COURSE_GATEWAY_URL", "GATEWAY_URL")


def before_all(context):
    import generate
    context.data = Path(tempfile.mkdtemp(prefix="simulated-runs-data-"))
    for name in ("SIM01", "SIM02", "SIM03", "SIM04"):
        generate.generate(name, out=context.data)
    context.saved_env = dict(os.environ)


def before_scenario(context, scenario):
    context.tmp = Path(tempfile.mkdtemp(prefix="simulated-runs-scenario-"))
    context.live = "live" in scenario.effective_tags
    if context.live:
        os.environ.setdefault("LEDGER", str(ROOT / "state" / "ledger.jsonl"))
        return
    os.environ.update(OFFLINE)
    for name in NOT_SET:
        os.environ.pop(name, None)
    os.environ["LEDGER"] = str(context.tmp / "ledger.jsonl")
    from moto import mock_aws
    context.moto = mock_aws()
    context.moto.start()


def after_scenario(context, scenario):
    if getattr(context, "moto", None):
        context.moto.stop()
        context.moto = None
    os.environ.clear()
    os.environ.update(context.saved_env)
    shutil.rmtree(context.tmp, ignore_errors=True)


def after_all(context):
    shutil.rmtree(context.data, ignore_errors=True)

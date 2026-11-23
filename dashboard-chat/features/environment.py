"""behave set-up for the scenarios in this folder. Run them from the recipe folder: `uv run behave`.

Every scenario runs offline: made-up AWS credentials, no course token, no gateway address. A scenario tagged @aws
(or in a feature tagged @aws) runs inside moto, a local stand-in for AWS, so its boto3 calls never leave the process.

Scenarios tagged @live: the eval comment-attacks, which sends its 48 questions to the course LLM gateway and reads
only the log it wrote itself, runs only when it is asked for (`uv run behave --tags=live`, with COURSE_GATEWAY_URL
and KB_MCP_TOKEN set); the scenarios of ext-athena-engine run in AWS with LIVE=1, after its deploy.py. Otherwise they
are skipped. `uv run behave` never reads a captured live log.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for folder in (ROOT / "scripts", ROOT / "ext-athena-engine", ROOT / "features", ROOT / "tests", ROOT / "ext-shiny-app", ROOT / "ext-feedback",
               ROOT / "ext-trace-alarms", ROOT / "ext-guardrails-redteam", ROOT / "ext-answer-cache", ROOT):
    sys.path.insert(0, str(folder))

OFFLINE = {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_SESSION_TOKEN": "testing",
           "AWS_DEFAULT_REGION": "ca-central-1", "AWS_REGION": "ca-central-1", "AWS_EC2_METADATA_DISABLED": "true",
           "PREFIX": "test"}
NOT_SET = ("AWS_PROFILE", "GATEWAY_URL", "COURSE_MCP_TOKEN", "COURSE_GATEWAY_URL", "KB_MCP_TOKEN", "CHAT_MODEL")


REAL_ENV = dict(os.environ)  # the shell's environment, for @live scenarios (ext-athena-engine)


def before_all(context):
    # The gateway address and the token, read once before they are removed, for the @live scenario only
    context.live_env = {name: os.environ[name] for name in NOT_SET if os.environ.get(name)}
    os.environ.update(OFFLINE)
    for name in NOT_SET:
        os.environ.pop(name, None)
    from main.data import data_version, make_data

    context.data_dir = Path(tempfile.mkdtemp(prefix="dashboard-chat-data-"))
    context.paths = make_data(context.data_dir)       # the same 1,200 orders and 4 stores on every machine
    context.version = data_version(context.paths)
    import make_data                                  # the rides tables of the comment cases (SIM01, SIM02)

    context.comment_dir = Path(tempfile.mkdtemp(prefix="dashboard-chat-comments-"))
    context.saved_comment_dir, make_data.DATA_DIR = make_data.DATA_DIR, context.comment_dir
    context.comment_paths = make_data.make(context.comment_dir)


def before_scenario(context, scenario):
    # Two kinds of @live scenario: ext-athena-engine's run in AWS (LIVE=1, after its deploy.py) with the shell's
    # environment; the eval comment-attacks calls the course LLM gateway when asked for (--tags=live) and reads the
    # gateway settings kept in before_all.
    live = "live" in scenario.effective_tags
    athena_live = live and Path(scenario.filename).name == "athena_engine.feature"
    if athena_live and os.environ.get("LIVE") != "1":
        context.moto, context.tmp = None, Path(tempfile.mkdtemp(prefix="dashboard-chat-scenario-"))
        scenario.skip("@live: runs in AWS with LIVE=1, after ext-athena-engine/deploy.py")
        return
    if athena_live:  # the shell's AWS credentials, PREFIX and gateway settings back
        os.environ.clear()
        os.environ.update(REAL_ENV)
    else:
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
    if live and not athena_live:
        asked = "live" in str(context.config.tag_expression)
        has_gateway = ({"COURSE_GATEWAY_URL", "GATEWAY_URL"} & set(context.live_env)
                       and {"KB_MCP_TOKEN", "COURSE_MCP_TOKEN"} & set(context.live_env))
        if not (asked and has_gateway):
            # 48 questions on the course LLM gateway, about 0.008 CAD: ext-guardrails-redteam/README.md, "Run it live"
            scenario.skip("@live calls the course LLM gateway: uv run behave --tags=live, with COURSE_GATEWAY_URL "
                          "and KB_MCP_TOKEN set")
            return
    if "aws" in scenario.effective_tags and not athena_live:
        from moto import mock_aws

        context.moto = mock_aws()
        context.moto.start()


def after_scenario(context, scenario):
    if context.moto is not None:
        context.moto.stop()
    shutil.rmtree(context.tmp, ignore_errors=True)


def after_all(context):
    import make_data

    make_data.DATA_DIR = context.saved_comment_dir
    shutil.rmtree(context.data_dir, ignore_errors=True)
    shutil.rmtree(context.comment_dir, ignore_errors=True)

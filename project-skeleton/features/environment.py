"""behave set-up for the scenarios in this folder. Run them from the recipe folder: `uv run behave`.

Every scenario runs offline, inside moto (a local stand-in for AWS), with made-up credentials and in a folder of
its own, so the `.local/` a run writes is the scenario's. A run with `--target aws` is called inside moto too, so
its boto3 calls never leave the process. Four things moto does not play are stood in for in features/support.py:

- the function URL: a signed request to it runs the handler of the package the recipe deployed, in this process;
- Firehose's delivery and Athena's answer: a scenario states what they return;
- the course LLM gateway: a scenario states what the model answers, and no model is called;
- a refused call or a resource that is still being deleted: put in where boto3 hands a call to the network.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for folder in (ROOT / "features", ROOT):
    sys.path.insert(0, str(folder))

OFFLINE = {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_SESSION_TOKEN": "testing",
           "AWS_DEFAULT_REGION": "ca-central-1", "AWS_EC2_METADATA_DISABLED": "true", "PREFIX": "recipe-ana"}
NOT_SET = ("AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_REGION", "GATEWAY_URL", "COURSE_MCP_TOKEN", "COURSE_GATEWAY_URL", "KB_MCP_TOKEN", "MODEL",
           "FIREHOSE_ROLE_ARN", "API_ROLE_ARN", "TABLE")


def before_all(context):
    import support

    logging.getLogger("botocore").setLevel(logging.WARNING)
    context.start_folder = os.getcwd()
    support.install_stand_ins()         # before any file of the recipe is imported: see support.no_waits
    from skeleton.cli import add_rep_paths

    add_rep_paths()             # the three rep-* folders, as every run.py adds them: a step may import from them


def before_scenario(context, scenario):
    import support
    from moto import mock_aws

    context.saved_env = dict(os.environ)
    os.environ.update(OFFLINE)
    for name in NOT_SET:
        os.environ.pop(name, None)
    context.tmp = Path(tempfile.mkdtemp(prefix="project-skeleton-scenario-"))
    os.chdir(context.tmp)                       # .local/ is written under the folder a run starts in
    context.moto = mock_aws()
    context.moto.start()
    context.calls = support.Calls()
    context.calls.start()
    support.reset()
    context.printed, context.exit_code, context.error = "", None, None
    context.sleeps, context.runs = support.WAITS, []


def after_scenario(context, scenario):
    context.calls.stop()
    context.moto.stop()
    os.chdir(context.start_folder)
    shutil.rmtree(context.tmp, ignore_errors=True)
    os.environ.clear()
    os.environ.update(context.saved_env)

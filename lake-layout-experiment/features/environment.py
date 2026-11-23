"""behave set-up: `uv run behave` from the recipe folder. Offline by default: made-up AWS credentials, the ledger in
a temporary folder, moto for scenarios tagged @aws; @live scenarios run in AWS only with LIVE=1 and are skipped
otherwise."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for folder in (ROOT / "duckdb_fn", ROOT / "scripts", ROOT):
    sys.path.insert(0, str(folder))

REAL_ENV = dict(os.environ)  # the shell's environment, given back to @live scenarios
OFFLINE = {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_SESSION_TOKEN": "testing",
           "AWS_DEFAULT_REGION": "ca-central-1", "AWS_REGION": "ca-central-1", "AWS_EC2_METADATA_DISABLED": "true",
           "PREFIX": "test"}


def before_scenario(context, scenario):
    context.tmp = Path(tempfile.mkdtemp(prefix="lake-layout-"))
    context.moto = None
    live = "live" in scenario.effective_tags
    if live and os.environ.get("LIVE") != "1":
        scenario.skip("@live: runs in AWS with LIVE=1")
        return
    if live:
        os.environ.clear()
        os.environ.update(REAL_ENV)
        return
    os.environ.update(OFFLINE)
    os.environ.pop("AWS_PROFILE", None)
    os.environ["LEDGER"] = str(context.tmp / "ledger.jsonl")
    if "aws" in scenario.effective_tags:
        from moto import mock_aws

        context.moto = mock_aws()
        context.moto.start()


def after_scenario(context, scenario):
    if context.moto is not None:
        context.moto.stop()
    shutil.rmtree(context.tmp, ignore_errors=True)

"""behave set-up for the scenarios in this folder. Run them from the recipe folder: `uv run behave`.

Every scenario runs offline. A script runs in one of three ways (features/support.py):
- dry: DRY=1, with an `aws` on the PATH that fails the scenario when it is called;
- fake: for real, with an `aws` that logs each call and answers what the scenario tells it to (tests/harness.py);
- local: for real, with the real AWS CLI pointed at a moto server on localhost. A feature tagged @moto starts the
  server; it is skipped when the AWS CLI is not installed.
Made-up credentials, no profile, temporary folders only.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for folder in (ROOT / "features", ROOT / "tests"):
    sys.path.insert(0, str(folder))

NOT_SET = ("AWS_PROFILE", "AWS_DEFAULT_PROFILE", "GATEWAY_URL", "COURSE_MCP_TOKEN", "COURSE_GATEWAY_URL", "KB_MCP_TOKEN", "KEEP", "DRY", "BUCKET", "PREFIX")


def before_all(context):
    for name in NOT_SET:
        os.environ.pop(name, None)


def before_feature(context, feature):
    context.moto_url = None
    context.shared = {}                    # runs a feature makes once and its scenarios read (support.shared_run)
    if "moto" in feature.tags:
        if not shutil.which("aws"):
            feature.skip("the AWS CLI is not installed; the scenarios that need no CLI still run")
            return
        from moto.server import ThreadedMotoServer

        context.moto_server = ThreadedMotoServer(ip_address="127.0.0.1", port=0)
        context.moto_server.start()
        host, port = context.moto_server.get_host_and_port()
        context.moto_url = f"http://{host}:{port}"
        import support

        support.reset_moto(context.moto_url)
        context.shared_tmp = Path(tempfile.mkdtemp(prefix="dynamodb-feature-"))


def after_feature(context, feature):
    if context.moto_url:
        context.moto_server.stop()
        shutil.rmtree(context.shared_tmp, ignore_errors=True)


def before_scenario(context, scenario):
    context.tmp = Path(tempfile.mkdtemp(prefix="dynamodb-scenario-"))
    context.run = None                     # the last script run: support.Run
    context.env = {}                       # extra environment for the runs of this scenario
    if context.moto_url and "fresh" in scenario.effective_tags:
        import support

        support.reset_moto(context.moto_url)
        context.shared = {}


def after_scenario(context, scenario):
    shutil.rmtree(context.tmp, ignore_errors=True)

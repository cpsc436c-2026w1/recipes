"""behave set-up for the scenarios in this folder. Run them from the recipe folder: `uv run behave`.

Every scenario runs offline: made-up AWS credentials, PREFIX "test", no AWS_PROFILE. Unless a scenario is tagged
@own_moto (a run with `--target local`, which starts moto itself), it runs inside moto, a local stand-in for
AWS, so its boto3 calls never leave the process; there `time.sleep` returns at once, and an Invoke of the function
runs its handler in-process (features/support.py says how that differs from Lambda). Nothing is written into the
recipe folder.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for folder in (ROOT / "features", ROOT, ROOT / "ext-runaway-loop", ROOT / "main"):
    sys.path.insert(0, str(folder))

OFFLINE = {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_SESSION_TOKEN": "testing",
           "AWS_DEFAULT_REGION": "ca-central-1", "AWS_REGION": "ca-central-1", "AWS_EC2_METADATA_DISABLED": "true",
           "PREFIX": "test"}
NOT_SET = ("AWS_PROFILE", "GATEWAY_URL", "COURSE_MCP_TOKEN", "COURSE_GATEWAY_URL", "KB_MCP_TOKEN", "ALARM_EMAIL", "LAMBDA_ROLE_ARN")


def before_scenario(context, scenario):
    from support import Calls

    os.environ.update(OFFLINE)
    for name in NOT_SET:
        os.environ.pop(name, None)
    context.printed = ""
    context.printed_err = ""
    context.exit_code = None
    context.error = None
    context.undo = []            # functions that put back what a scenario patched
    context.calls = Calls()      # every AWS call the recipe made, and the answers a scenario chose for some
    context.calls.install(context)
    context.moto = None
    if "own_moto" in scenario.effective_tags:
        from support import refuse_network

        refuse_network(context)  # the script starts moto itself; nothing may leave the process
    else:
        from moto import mock_aws

        from support import no_waiting, run_function_in_process

        context.moto = mock_aws()
        context.moto.start()
        no_waiting(context)
        run_function_in_process(context)
        # moto has no GetAccountSettings for Lambda. The default is a total of 400, 200 of them unreserved, as a
        # course sandbox reported on 2026-11-12 (output/main.txt shows only that 5 could be reserved there).
        context.calls.answer("GetAccountSettings", reply={"AccountLimit": {"ConcurrentExecutions": 400,
                                                                           "UnreservedConcurrentExecutions": 200}})


def after_scenario(context, scenario):
    for undo in reversed(context.undo):
        undo()
    if context.moto is not None:
        context.moto.stop()

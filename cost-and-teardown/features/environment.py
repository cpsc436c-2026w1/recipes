"""behave set-up: every scenario offline, inside moto, as team t00 (owner ana) in the account 111122223333, with
its own ledger, docs/resources.md and evidence folder. features/support.py writes down every AWS call and answers
the calls a scenario names (a refusal, the Tagging API's answer for Kinesis, which moto does not cover)."""
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for folder in (ROOT / "features", ROOT / "scripts", ROOT / "sim", ROOT):
    sys.path.insert(0, str(folder))

OFFLINE = {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_SESSION_TOKEN": "testing",
           "AWS_DEFAULT_REGION": "ca-central-1", "AWS_EC2_METADATA_DISABLED": "true", "MOTO_ACCOUNT_ID": "111122223333",
           "PREFIX": "t00", "TEAM": "t00", "OWNER": "ana"}
NOT_SET = ("AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_REGION", "PURPOSE", "KB_MCP_TOKEN", "COURSE_MCP_TOKEN")


def before_all(context):
    logging.getLogger("botocore").setLevel(logging.WARNING)
    import support
    support.no_sleep()


def before_scenario(context, scenario):
    import support
    from moto import mock_aws

    context.saved_env = dict(os.environ)
    os.environ.update(OFFLINE)
    for name in NOT_SET:
        os.environ.pop(name, None)
    context.tmp = Path(tempfile.mkdtemp(prefix="cost-and-teardown-"))
    os.environ.update(LEDGER=str(context.tmp / "state" / "ledger.jsonl"),
                      RESOURCES_DOC=str(context.tmp / "docs" / "resources.md"), EVIDENCE=str(context.tmp / "evidence"))
    context.moto = mock_aws()
    context.moto.start()
    context.calls = support.Calls()
    context.calls.start()
    context.calls.kinesis_in_tagging_api()
    context.printed, context.exit_code = "", None


def after_scenario(context, scenario):
    context.calls.stop()
    context.moto.stop()
    shutil.rmtree(context.tmp, ignore_errors=True)
    os.environ.clear()
    os.environ.update(context.saved_env)

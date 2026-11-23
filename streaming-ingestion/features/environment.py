"""behave set-up for the scenarios in this folder. Run them from the recipe folder: `uv run behave`.

Every scenario runs offline, inside moto (a local stand-in for AWS), with made-up credentials. A run with
`--target aws` is called inside moto too, so its boto3 calls never leave the process.

Lambda: moto would start a Docker container to run a function. Here it does not: support.run_function_here takes
the package the recipe deployed (the zip), loads its handler in this process and calls it. So the code that runs is
the code that was deployed, with the environment variables the recipe gave the function, and no container starts.

Stream records: moto would call the function once per record at the moment the record is put, and ignore what the
function returns. That is not what Lambda does, so moto's hand-over is switched off and support.poll_streams does
it: batches of the mapping's size, the records before a reported failure counted as done, the rest retried up to
the mapping's limit, then the batch's details to the on-failure queue. It does not play the splitting of a failing
batch (BisectBatchOnFunctionError): after the retries it gives up on every record from the failed one on, where
Lambda may split a batch whose function raises. Numbers of stored updates in a failing scenario are this stand-in's.

What moto does not play (a refused call, a resource that is still being deleted, a stream that refuses records) is
put in at the one place where boto3 hands a call to the network (features/support.py, Calls).
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for folder in (ROOT / "features", ROOT / "tests", ROOT / "ext-fan-out", ROOT / "ext-windowed-aggregates",
               ROOT / "ext-queue-ingest", ROOT / "ext-source-poller", ROOT / "sim", ROOT / "scripts", ROOT / "src",
               ROOT / "main", ROOT):
    sys.path.insert(0, str(folder))

OFFLINE = {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_SESSION_TOKEN": "testing",
           "AWS_DEFAULT_REGION": "ca-central-1", "AWS_EC2_METADATA_DISABLED": "true", "PREFIX": "recipe-ana"}
NOT_SET = ("AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_REGION", "GATEWAY_URL", "COURSE_MCP_TOKEN", "COURSE_GATEWAY_URL", "KB_MCP_TOKEN", "LAMBDA_ROLE_ARN",
           "TABLE", "LATE_RULE", "CLOSE_DELAY_S", "BATCH_WINDOW_S", "SINK", "BUCKET", "TEAM", "OWNER", "PURPOSE")


def before_all(context):
    import support
    from moto.awslambda import models as moto_lambda

    logging.getLogger("botocore").setLevel(logging.WARNING)
    moto_lambda.LambdaFunction._invoke_lambda = support.run_function_here
    moto_lambda.LambdaBackend.send_kinesis_message = lambda self, *args, **kwargs: None   # support.poll_streams
    # moto's SQS checks every message's visibility on each receive with a clock helper that builds two datetimes
    # per call; with the 2,515 messages of a simulated hour that is most of a scenario's time. The same clock, read
    # once per call (epoch milliseconds, as moto's helper returns):
    import time as _time
    from moto.sqs import models as moto_sqs

    moto_sqs.unix_time_millis = lambda: _time.time() * 1000.0
    moto_sqs.unix_time = lambda: _time.time()


def before_scenario(context, scenario):
    import support
    from moto import mock_aws

    if "live" in scenario.effective_tags:
        if not os.environ.get("LIVE"):
            scenario.skip("@live: runs in AWS only (LIVE=1 uv run behave --tags=live, after the README's Run it)")
        context.printed, context.exit_code, context.error = "", None, None
        return                                               # a live scenario talks to AWS: no moto, no stand-ins
    os.environ.update(OFFLINE)
    for name in NOT_SET:
        os.environ.pop(name, None)
    if "prefix_recipe" in scenario.effective_tags:      # the two newest extensions' scenarios name recipe-...
        os.environ["PREFIX"] = "recipe"
    context.tmp = Path(tempfile.mkdtemp(prefix="streaming-ingestion-scenario-"))
    os.environ["LEDGER"] = str(context.tmp / "ledger.jsonl")       # scripts/tags.py: never the folder's own ledger
    context.moto = mock_aws()
    context.moto.start()
    context.calls = support.Calls()
    context.calls.start()
    support.FUNCTION_RUNS.clear()
    support.MAPPINGS.clear(), support.CHECKPOINTS.clear(), support.GIVEN_UP.clear()
    support._MODULES.clear()         # a function's module keeps its boto3 client: a new scenario, a new environment
    context.printed, context.exit_code, context.error = "", None, None
    context.sleeps = []


def after_scenario(context, scenario):
    if "live" in scenario.effective_tags:
        return
    context.calls.stop()
    context.moto.stop()
    shutil.rmtree(context.tmp, ignore_errors=True)

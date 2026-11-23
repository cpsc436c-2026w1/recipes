"""behave set-up: every scenario offline (moto), PREFIX recipe, its own ledger and DuckDB table, except @live ones,
which run in your sandbox (`uv run behave --tags=@live`). features/support.py stands in for Glue's StartJobRun and
GetJobRun, and for Athena's MERGE, with the DuckDB load of pipeline/local_load.py."""
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for folder in (ROOT / "features", ROOT / "scripts", ROOT / "pipeline", ROOT / "sim", ROOT / "src", ROOT):
    sys.path.insert(0, str(folder))

OFFLINE = {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_SESSION_TOKEN": "testing",
           "AWS_DEFAULT_REGION": "ca-central-1", "AWS_EC2_METADATA_DISABLED": "true", "PREFIX": "recipe",
           "TEAM": "recipe", "OWNER": "ana"}
NOT_SET = ("AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_REGION", "PURPOSE", "ENGINE", "ETL_MIN_GAP_MIN",
           "MAX_RUNS_PER_DAY", "ETL_FAULT", "ETL_FAULT_TIMEOUT_MIN", "KB_MCP_TOKEN", "COURSE_MCP_TOKEN")


def before_all(context):
    logging.getLogger("botocore").setLevel(logging.WARNING)
    if not (ROOT / "sim" / "data" / "SIM01" / "_truth" / "events.jsonl").exists():   # sim/data/ is not committed
        import subprocess
        subprocess.run([sys.executable, str(ROOT / "sim" / "generate.py"), "SIM01"], check=True,
                       stdout=subprocess.DEVNULL)


def before_scenario(context, scenario):
    import control
    import support
    from moto import mock_aws

    context.saved_env = dict(os.environ)
    context.live = "live" in scenario.effective_tags
    context.real_now = control.now
    context.tmp = Path(tempfile.mkdtemp(prefix="s3-event-etl-"))
    if not context.live:
        os.environ.update(OFFLINE)
        for name in NOT_SET:
            os.environ.pop(name, None)
        os.environ.update(LEDGER=str(context.tmp / "ledger.jsonl"), ETL_LOCAL_DB=str(context.tmp / "lake.duckdb"))
        context.moto = mock_aws()
        context.moto.start()
        context.calls = support.Calls()
        context.calls.start()
        context.glue = support.GlueStub(context)
    context.printed = ""


def after_scenario(context, scenario):
    import control
    control.now = context.real_now
    if not context.live:
        context.calls.stop()
        context.moto.stop()
    shutil.rmtree(context.tmp, ignore_errors=True)
    os.environ.clear()
    os.environ.update(context.saved_env)

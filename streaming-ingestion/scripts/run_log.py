"""The run log `<PREFIX>-runs`: one DynamoDB item per run of a scheduled job.

    from run_log import ensure_run_log
    table = ensure_run_log()          # "<PREFIX>-runs", created if it is missing

Key `job` (string), sort key `run_id` (string); on-demand capacity. Attributes the recipes write: `window_start`,
`window_end`, `run_state` (`running`, `finished`, `failed`, `skipped`), `failed_step`, `error_text`, `merge_query_id`,
`rebuild_query_id`, `objects`, `quarantined`, `bad_lines`, `old_events`, `checked_count`, `started_at`, `ended_at`;
items `lock` (`lock_until`) and `last-finished`. No attribute is named with a DynamoDB reserved word (`error`,
`status`, `count`, `day`, `table`), so no update needs a name placeholder.

Every recipe that writes the run log calls ensure_run_log(): the first one creates it and records it `created`;
a table that was there already is recorded `borrowed`. One with another key schema stops with an error.

This file is a shared helper (tools/shared_helpers.toml): copy it unchanged into `scripts/run_log.py`.
"""
from __future__ import annotations

import sys
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tags import prefix, record, recorded, tags  # noqa: E402

REGION = "ca-central-1"
KEY_SCHEMA = [{"AttributeName": "job", "KeyType": "HASH"}, {"AttributeName": "run_id", "KeyType": "RANGE"}]


def run_log_name() -> str:
    return f"{prefix()}-runs"


def ensure_run_log(sess=None) -> str:
    """Create `<PREFIX>-runs` if it is missing and wait until it is active; check its keys if it exists."""
    sess = sess or boto3.session.Session(region_name=REGION)
    ddb, name = sess.client("dynamodb"), run_log_name()
    try:
        table = ddb.describe_table(TableName=name)["Table"]
    except ClientError as err:
        if err.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
        table = None
    if table is not None:
        if table["KeySchema"] != KEY_SCHEMA:
            raise RuntimeError(f"{name} exists with the key schema {table['KeySchema']}, not job and run_id")
        if recorded("dynamodb-table", name) not in ("created", "borrowed"):
            record("dynamodb-table", name, "borrowed", arn=table["TableArn"], script="run_log.py ensure_run_log")
        if table["TableStatus"] != "ACTIVE":
            ddb.get_waiter("table_exists").wait(TableName=name)
        return name
    made = ddb.create_table(
        TableName=name, KeySchema=KEY_SCHEMA, BillingMode="PAY_PER_REQUEST", Tags=tags("dynamodb"),
        AttributeDefinitions=[{"AttributeName": "job", "AttributeType": "S"},
                              {"AttributeName": "run_id", "AttributeType": "S"}])
    record("dynamodb-table", name, "created", arn=made["TableDescription"]["TableArn"],
           script="run_log.py ensure_run_log", clean="the clean_up.py of the recipe whose ledger has this row")
    ddb.get_waiter("table_exists").wait(TableName=name, WaiterConfig={"Delay": 2, "MaxAttempts": 60})
    return name

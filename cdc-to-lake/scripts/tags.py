"""The course tags for every create call, and the creation ledger `state/ledger.jsonl`.

    tags("s3")     -> [{"Key": "Project", "Value": "436c"}, {"Key": "Team", ...}, {"Key": "Owner", ...}]
    tags("lambda") -> {"Project": "436c", "Team": ..., "Owner": ...}
    record("s3-bucket", "recipe-lake-111122223333", "created", script="scripts/lake.py up", clean="scripts/lake.py down")

Tags: Project=436c, Team=$TEAM (default $PREFIX), Owner=$OWNER (default your login name), and purpose=$PURPOSE when
it is set. Each service takes them in its own shape: a list of
{Key, Value} for most, a map for Lambda, CloudWatch Logs, SQS, Kinesis, Glue and API Gateway, {key, value} for ECS.
The Scheduler tags only schedule groups, so a recipe makes its schedules in a group tagged at creation.

The ledger has one JSON line per create (`created`), per resource that was there before (`borrowed`) and per delete
(`deleted`); the last line for a type and name is its state. Files written into the shared lake bucket are recorded
too (`s3-replay` with a run id, `s3-object`, `s3-prefix`), so `scripts/lake.py down` deletes those and no others. A team commits it, so every member's creates are on it;
the recipes repository ignores it (it holds ARNs with the account number).

This file is a shared helper (tools/shared_helpers.toml): copy it unchanged into `scripts/tags.py`.
"""
from __future__ import annotations

import getpass
import json
import os
from datetime import datetime, timezone
from pathlib import Path

MAP_SERVICES = {"lambda", "logs", "sqs", "kinesis", "glue", "apigateway", "apigatewayv2"}
LOWER_SERVICES = {"ecs"}
STATES = ("created", "borrowed", "deleted")


def prefix() -> str:
    """The name prefix of every resource: $PREFIX, default `recipe`."""
    return os.environ.get("PREFIX") or "recipe"


def tag_values() -> dict[str, str]:
    """The course tags as a plain map."""
    values = {"Project": "436c", "Team": os.environ.get("TEAM") or prefix(),
              "Owner": os.environ.get("OWNER") or getpass.getuser()}
    if os.environ.get("PURPOSE"):
        values["purpose"] = os.environ["PURPOSE"]
    return values


def tags(service: str) -> list[dict] | dict:
    """The course tags in the shape the service's create call takes (the boto3 client name: "s3", "lambda" ...)."""
    values = tag_values()
    if service in MAP_SERVICES:
        return dict(values)
    if service in LOWER_SERVICES:
        return [{"key": k, "value": v} for k, v in values.items()]
    return [{"Key": k, "Value": v} for k, v in values.items()]


def ledger_path() -> Path:
    """`state/ledger.jsonl` of the recipe folder (this file is in scripts/), or $LEDGER."""
    return Path(os.environ.get("LEDGER") or Path(__file__).resolve().parent.parent / "state" / "ledger.jsonl")


def record(type: str, name: str, state: str = "created", *, arn: str = "", script: str = "", clean: str = "",
           bills: str = "per use", ledger: Path | None = None) -> dict:
    """Append one line to the ledger: what was made (or found, or deleted), by which script, how it is cleaned up
    and how it bills ("per use", "hourly", "monthly")."""
    if state not in STATES:
        raise ValueError(f"state must be one of {STATES}, not {state!r}")
    row = {"time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "type": type, "name": name,
           "state": state, "arn": arn, "script": script, "clean": clean, "bills": bills, "prefix": prefix()}
    path = ledger or ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(row, sort_keys=True) + "\n")
    return row


def ledger_rows(ledger: Path | None = None) -> list[dict]:
    """Every line of the ledger, oldest first; an empty list when there is none yet."""
    path = ledger or ledger_path()
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def recorded(type: str, name: str, ledger: Path | None = None) -> str | None:
    """The state of the last ledger line for this type and name (created, borrowed, deleted), or None."""
    rows = [r for r in ledger_rows(ledger) if r["type"] == type and r["name"] == name]
    return rows[-1]["state"] if rows else None

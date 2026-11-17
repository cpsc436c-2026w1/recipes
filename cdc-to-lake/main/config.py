"""Settings, all from the environment. Nothing secret is written in code or in the repo.

PREFIX               first part of every AWS resource name (default "recipe")
AWS_REGION           default ca-central-1
BUCKET               the S3 bucket for the lake; default {PREFIX}-cdc-lake-{account id}
LAMBDA_ROLE_ARN      an existing role for the Lambda function (sandboxes may not let you create roles)
FIREHOSE_ROLE_ARN    an existing role Firehose assumes to write the Iceberg table
SCHEDULER_ROLE_ARN   an existing role EventBridge Scheduler assumes (ext-maintenance only)
ATHENA_WORKGROUP     default primary
BUFFER_SECONDS       how long Firehose collects records before it writes (default 60)
LAG_LIMIT_SECONDS    the change-to-queryable limit the alarm checks (ext-lag-alarm; default 300)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SQL_DIR = ROOT / "main" / "sql"
OUTPUT_DIR = ROOT / "output"


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    prefix: str = field(default_factory=lambda: _env("PREFIX", "recipe"))
    region: str = field(default_factory=lambda: _env("AWS_REGION", "ca-central-1"))
    bucket_env: str = field(default_factory=lambda: _env("BUCKET"))
    lambda_role_arn: str = field(default_factory=lambda: _env("LAMBDA_ROLE_ARN"))
    firehose_role_arn: str = field(default_factory=lambda: _env("FIREHOSE_ROLE_ARN"))
    scheduler_role_arn: str = field(default_factory=lambda: _env("SCHEDULER_ROLE_ARN"))
    workgroup: str = field(default_factory=lambda: _env("ATHENA_WORKGROUP", "primary"))
    # Firehose's buffer interval is the largest part of the lag from change to queryable: a row is not in the
    # table until its buffer is written. 60 s keeps a demo quick; a shorter interval writes more, smaller files
    # (see ext-maintenance). Firehose takes 0 to 900 s (API reference, BufferingHints).
    buffer_seconds: int = field(default_factory=lambda: int(_env("BUFFER_SECONDS", "60")))
    lag_limit_seconds: int = field(default_factory=lambda: int(_env("LAG_LIMIT_SECONDS", "300")))

    def bucket(self, account: str) -> str:
        return self.bucket_env or f"{self.prefix}-cdc-lake-{account}"

    @property
    def table(self) -> str:  # DynamoDB table
        return f"{self.prefix}-trips"

    @property
    def database(self) -> str:  # Glue database: lower case, underscores (Athena does not like hyphens)
        return f"{self.prefix.replace('-', '_')}_cdc"

    lake_table: str = "trip_changes"  # Iceberg table, the change log
    view: str = "trips_current"       # one row per trip, its latest state

    @property
    def function(self) -> str:
        return f"{self.prefix}-cdc-to-firehose"

    @property
    def delivery_stream(self) -> str:
        return f"{self.prefix}-trip-changes"

    @property
    def failures_queue(self) -> str:  # where Lambda records the batches it gave up on
        return f"{self.prefix}-cdc-failures"

    def location(self, account: str) -> str:
        return f"s3://{self.bucket(account)}/lake/{self.lake_table}/"

    def athena_output(self, account: str) -> str:
        return f"s3://{self.bucket(account)}/athena-results/"


def offline_env(region: str = "ca-central-1") -> None:
    """Fake credentials for moto, so an offline run can never reach a real account."""
    for k, v in {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_SESSION_TOKEN": "testing",
                 "AWS_DEFAULT_REGION": region}.items():
        os.environ[k] = v
    os.environ.pop("AWS_PROFILE", None)


def sql(name: str, **names) -> str:
    """Read main/sql/<name>.sql and fill in the table names. One SQL text runs in Athena and in DuckDB."""
    return (SQL_DIR / f"{name}.sql").read_text().format(**names)

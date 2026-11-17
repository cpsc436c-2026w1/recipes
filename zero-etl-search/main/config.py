"""Settings, all from the environment. Nothing secret is written in code or in the repo.

PREFIX               first part of every AWS resource name (default "recipe")
AWS_REGION           default ca-central-1
LAMBDA_ROLE_ARN      an existing role for the indexing function (main/infra.py lists what it must be allowed to do);
                     created otherwise, as $PREFIX-search-indexer-role
INDEX_VERSION        the version of the keyword index's mappings (default 1): index name items-v{INDEX_VERSION}

ext-zero-etl only:
PIPELINE_ROLE_ARN    an existing role the OpenSearch Ingestion pipeline assumes
BUCKET               the S3 bucket for the table export and the pipeline's dead-letter files;
                     default {PREFIX}-search-{account id}
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


@dataclass(frozen=True)
class Settings:
    prefix: str = field(default_factory=lambda: os.environ.get("PREFIX", "recipe"))
    region: str = field(default_factory=lambda: os.environ.get("AWS_REGION", "ca-central-1"))
    lambda_role_arn: str = field(default_factory=lambda: os.environ.get("LAMBDA_ROLE_ARN", ""))
    pipeline_role_arn: str = field(default_factory=lambda: os.environ.get("PIPELINE_ROLE_ARN", ""))
    bucket_env: str = field(default_factory=lambda: os.environ.get("BUCKET", ""))
    index_version: int = field(default_factory=lambda: int(os.environ.get("INDEX_VERSION", "1")))

    @property
    def table(self) -> str:
        return f"{self.prefix}-items"

    @property
    def function(self) -> str:  # the Lambda function that keeps the index in step with the table
        return f"{self.prefix}-search-indexer"

    @property
    def log_group(self) -> str:
        return f"/aws/lambda/{self.function}"

    @property
    def alarm_iterator_age(self) -> str:  # CloudWatch alarm: the function has fallen behind the stream
        return f"{self.prefix}-search-iterator-age"

    @property
    def alarm_failed_records(self) -> str:  # CloudWatch alarm: a record is in the failure queue
        return f"{self.prefix}-search-failed-records"

    @property
    def failure_queue(self) -> str:  # where Lambda puts a batch it gave up on (README, Decisions: one bad record)
        return f"{self.prefix}-search-indexer-failed"

    @property
    def group(self) -> str:  # the keyword collection's group: it holds the capacity and is billed
        return f"{self.prefix}-search"

    @property
    def vector_group(self) -> str:  # ext-embeddings: a group holds one type of collection, so vectors get their own
        return f"{self.prefix}-search-vectors"

    @property
    def collection(self) -> str:  # 3 to 64 characters, a lower-case letter first (boto3 service model)
        return f"{self.prefix}-items"

    @property
    def vector_collection(self) -> str:  # ext-embeddings
        return f"{self.prefix}-item-vectors"

    @property
    def pipeline(self) -> str:  # ext-zero-etl. OpenSearch Ingestion: 3 to 28 characters, starts with a letter
        return f"{self.prefix}-items-sync"[:28]

    @property
    def index(self) -> str:
        # The version is part of the name: a mapping change builds items-v2 beside items-v1, and the app moves to
        # it when it is full (README, "Adapt it"). An index's mappings cannot change type in place.
        return f"items-v{self.index_version}"

    def bucket(self, account: str) -> str:  # ext-zero-etl
        return self.bucket_env or f"{self.prefix}-search-{account}"


def offline_env(region: str = "ca-central-1") -> None:
    """Fake credentials for moto, so an offline run can never reach a real account."""
    for k, v in {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_SESSION_TOKEN": "testing",
                 "AWS_DEFAULT_REGION": region}.items():
        os.environ[k] = v
    os.environ.pop("AWS_PROFILE", None)


def load_items() -> list[dict]:
    return [json.loads(line) for line in (DATA / "items.jsonl").read_text().splitlines() if line.strip()]

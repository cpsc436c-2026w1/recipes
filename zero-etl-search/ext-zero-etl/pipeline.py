"""ext-zero-etl: the OpenSearch Ingestion pipeline: DynamoDB table -> (initial export, then the stream) -> the index.

render() builds the pipeline configuration (YAML, Data Prepper format "2") from the settings;
ext-zero-etl/pipeline.example.yaml is render() with example values, kept in the repo to read
(tests/test_zero_etl.py checks it is current). validate() is the offline check; on AWS, ext-zero-etl/zero_etl.py
also calls the service's own ValidatePipeline before CreatePipeline.

Shape from the AWS docs, "DynamoDB pipeline blueprint" (OpenSearch Service developer guide), with an OpenSearch
Serverless collection as the sink.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from main.config import ROOT, Settings  # noqa: E402

EXAMPLE = ROOT / "ext-zero-etl" / "pipeline.example.yaml"
REQUIRED_SINK_EXPRESSIONS = {
    # One document per item: the item's key is the document id, so an update replaces the document.
    "document_id": '${getMetadata("primary_key")}',
    # INSERT and MODIFY become index requests, REMOVE becomes a delete.
    "action": '${getMetadata("opensearch_action")}',
    # External versioning: a change older than the document already indexed is ignored, so an event that
    # arrives late (a retried export file, say) cannot overwrite a newer change.
    "document_version": '${getMetadata("document_version")}',
    "document_version_type": "external",
}


def render(s: Settings, table_arn: str, endpoint: str, bucket: str) -> str:
    aws = {"sts_role_arn": s.pipeline_role_arn, "region": s.region}
    config = {
        "version": "2",
        s.pipeline: {
            "source": {"dynamodb": {
                "acknowledgments": True,  # a stream position is marked done only after OpenSearch has the documents
                "tables": [{
                    "table_arn": table_arn,
                    # The initial export copies the whole table from point-in-time recovery (no read units used);
                    # the stream then carries every change made after the export's time.
                    "export": {"s3_bucket": bucket, "s3_region": s.region, "s3_prefix": f"{s.prefix}/ddb-export/"},
                    "stream": {"start_position": "LATEST"},
                }],
                "aws": aws,
            }},
            "processor": [
                # Every document says which index version wrote it (README, "Decisions": version recorded).
                {"add_entries": {"entries": [{"key": "index_version", "value": s.index_version}]}},
            ],
            "sink": [{"opensearch": {
                "hosts": [endpoint],
                "index": s.index,
                "index_type": "custom",
                **REQUIRED_SINK_EXPRESSIONS,
                "aws": {**aws, "serverless": True},
                # Documents OpenSearch rejects go to S3 instead of being dropped or blocking the pipeline.
                "dlq": {"s3": {"bucket": bucket, "key_path_prefix": f"{s.prefix}/osis-dlq/", "region": s.region,
                               "sts_role_arn": s.pipeline_role_arn}},
            }}],
        },
    }
    return yaml.safe_dump(config, sort_keys=False, width=120)


def example_settings() -> tuple[Settings, dict]:
    s = Settings(prefix="recipe", region="ca-central-1",
                 pipeline_role_arn="arn:aws:iam::111122223333:role/recipe-pipeline", index_version=1)
    return s, {"table_arn": "arn:aws:dynamodb:ca-central-1:111122223333:table/recipe-items",
               "endpoint": "https://abcdefghij0123456789.ca-central-1.aoss.amazonaws.com",
               "bucket": "recipe-search-111122223333"}


def validate(text: str, s: Settings) -> list[str]:
    """Offline checks of a rendered pipeline. Empty list = valid."""
    errors = []
    doc = yaml.safe_load(text)
    if doc.get("version") != "2":
        errors.append('version must be "2"')
    names = [k for k in doc if k != "version"]
    if len(names) != 1:
        return errors + ["exactly one pipeline per configuration"]
    name = names[0]
    if not re.fullmatch(r"[a-z][a-z0-9-]{2,27}", name):
        errors.append(f"pipeline name {name!r}: 3 to 28 lower-case letters, digits, hyphens, starting with a letter")
    p = doc[name]
    tables = p.get("source", {}).get("dynamodb", {}).get("tables", [])
    if len(tables) != 1:
        errors.append("a DynamoDB source reads exactly one table (AWS docs, limitations)")
    for t in tables:
        if not re.fullmatch(rf"arn:aws:dynamodb:{s.region}:\d{{12}}:table/[A-Za-z0-9_.-]+", t.get("table_arn", "")):
            errors.append("table_arn must be a table in this region (no cross-Region ingestion)")
        if "export" not in t or "stream" not in t:
            errors.append("the table needs both export (initial copy) and stream (changes after it)")
    sinks = p.get("sink", [])
    if len(sinks) != 1 or "opensearch" not in sinks[0]:
        return errors + ["exactly one opensearch sink"]
    sink = sinks[0]["opensearch"]
    if sink.get("index") != s.index:
        errors.append(f"sink index {sink.get('index')!r} is not the versioned index {s.index!r}")
    for key, value in REQUIRED_SINK_EXPRESSIONS.items():
        if sink.get(key) != value:
            errors.append(f"sink {key} must be {value}")
    if sink.get("aws", {}).get("serverless") is not True:
        errors.append("sink aws.serverless must be true for an OpenSearch Serverless collection")
    if not str(sink.get("hosts", [""])[0]).startswith("https://"):
        errors.append("sink host must be the collection's https endpoint")
    if re.search(r"(?i)(secret|password|api_key|access_key)\s*:", text):
        errors.append("no credentials in a pipeline configuration: the pipeline uses its role")
    return errors


if __name__ == "__main__":
    # `uv run python ext-zero-etl/pipeline.py --write` renders the example again after a change to render().
    s, ex = example_settings()
    text = render(s, **ex)
    if "--write" in sys.argv:
        head = EXAMPLE.read_text().split("\n", 1)[0]
        EXAMPLE.write_text(head + "\n" + text)
    print(text, end="")

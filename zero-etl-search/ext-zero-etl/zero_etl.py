"""ext-zero-etl: the same index fed by an OpenSearch Ingestion pipeline instead of the Lambda function.

  uv run python ext-zero-etl/zero_etl.py                  offline: validate the pipeline, moto table, stand-ins
  uv run python ext-zero-etl/zero_etl.py --target aws     an account that allows OpenSearch Ingestion (osis:*)

The course sandbox denies osis:* in a service control policy (output/main-aws-denied.txt): there, --target aws stops
at ValidatePipeline. Run clean_up.py after it all the same (it removes the table, the bucket and the collection).

Written from the AWS docs ("DynamoDB pipeline blueprint", "Getting started with OpenSearch Ingestion and
Serverless") and the boto3 service models; the pipeline steps have not run in a sandbox. The pipeline role is created
by ensure_role() with only what the pipeline does (SEC03-BP02). To use a role you already have, set
PIPELINE_ROLE_ARN; it must trust osis-pipelines.amazonaws.com and be allowed:
  dynamodb  DescribeTable, DescribeContinuousBackups, ExportTableToPointInTime on the table; DescribeExport on
            table/<name>/export/*; DescribeStream, GetRecords, GetShardIterator on table/<name>/stream/*
  s3        GetObject, PutObject, AbortMultipartUpload, PutObjectAcl on <bucket>/<PREFIX>/ddb-export/* and <bucket>/<PREFIX>/osis-dlq/*
  aoss      BatchGetCollection, APIAccessAll on the collection
"""

from __future__ import annotations

import argparse
import dataclasses
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import boto3  # noqa: E402

from main.config import Settings, load_items, offline_env  # noqa: E402
from main.mappings import index_body, keyword_query, validate_index  # noqa: E402
from pipeline import example_settings, render, validate  # noqa: E402

# OpenSearch Ingestion units, minimum and maximum. 1 is the smallest minimum the service allows; AWS recommends 2
# for availability (AWS docs, "Scaling pipelines"). Billed 0.253 USD per unit-hour in ca-central-1 for every minute
# the pipeline runs, with or without changes to carry (AWS bulk price list, read 2026-11-12).
PIPELINE_UNITS = (1, 2)
CHANGED = "I04"
CHANGE = {"name": "Headlamp HX-300", "description": "Rechargeable headlamp, 600 lumens, red night mode, waterproof",
          "in_stock": True}
QUERY = "waterproof headlamp"


def pipeline_statements(s: Settings, account: str, table_arn: str, bucket: str) -> list[dict]:
    """What the pipeline may do (AWS docs, "DynamoDB pipeline blueprint"; not yet run in a sandbox)."""
    return [
        {"Effect": "Allow", "Resource": table_arn,
         "Action": ["dynamodb:DescribeTable", "dynamodb:DescribeContinuousBackups",
                    "dynamodb:ExportTableToPointInTime"]},
        {"Effect": "Allow", "Action": ["dynamodb:DescribeExport"], "Resource": f"{table_arn}/export/*"},
        {"Effect": "Allow", "Resource": f"{table_arn}/stream/*",
         "Action": ["dynamodb:DescribeStream", "dynamodb:GetRecords", "dynamodb:GetShardIterator"]},
        {"Effect": "Allow", "Resource": [f"arn:aws:s3:::{bucket}/{s.prefix}/ddb-export/*",
                                  f"arn:aws:s3:::{bucket}/{s.prefix}/osis-dlq/*"],
         "Action": ["s3:GetObject", "s3:PutObject", "s3:AbortMultipartUpload", "s3:PutObjectAcl"]},
        # the collection's ARN is known only after it exists; scoped to this account's collections in the region
        {"Effect": "Allow", "Action": ["aoss:BatchGetCollection", "aoss:APIAccessAll"],
         "Resource": f"arn:aws:aoss:{s.region}:{account}:collection/*"},
    ]


def create_bucket(s3, bucket: str, region: str) -> None:
    """The export's files and the pipeline's dead-letter files. Private: every public access setting blocked."""
    try:
        s3.create_bucket(Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": region})
    except s3.exceptions.BucketAlreadyOwnedByYou:
        pass
    s3.put_public_access_block(Bucket=bucket, PublicAccessBlockConfiguration={
        "BlockPublicAcls": True, "IgnorePublicAcls": True, "BlockPublicPolicy": True, "RestrictPublicBuckets": True})


def _pipeline_status(osis, name: str) -> str:
    try:
        return osis.get_pipeline(PipelineName=name)["Pipeline"]["Status"]
    except osis.exceptions.ResourceNotFoundException:
        return ""


def create_pipeline(osis, s: Settings, config: str, wait_s: int = 1200) -> None:
    errors = validate(config, s)
    if errors:
        raise SystemExit("pipeline configuration invalid:\n  " + "\n  ".join(errors))
    check = osis.validate_pipeline(PipelineConfigurationBody=config)  # the service's own check
    if not check.get("isValid"):
        raise SystemExit(f"OpenSearch Ingestion refused the configuration: {check.get('Errors')}")
    deadline = time.time() + wait_s
    # A pipeline that clean_up.py is still deleting answers "already exists" to a create, and then disappears.
    while _pipeline_status(osis, s.pipeline) == "DELETING" and time.time() < deadline:
        time.sleep(20)
    try:
        # The pipeline bills from ACTIVE until it is stopped or deleted, also while there is nothing to carry.
        osis.create_pipeline(PipelineName=s.pipeline, MinUnits=PIPELINE_UNITS[0], MaxUnits=PIPELINE_UNITS[1],
                             PipelineConfigurationBody=config,
                             Tags=[{"Key": "recipe", "Value": "zero-etl-search"}])
    except osis.exceptions.ResourceAlreadyExistsException:
        pass
    while time.time() < deadline:
        status = osis.get_pipeline(PipelineName=s.pipeline)["Pipeline"]["Status"]
        if status == "ACTIVE":
            return
        if status.endswith("FAILED"):
            raise RuntimeError(f"pipeline {s.pipeline}: {status}")
        time.sleep(20)
    raise TimeoutError(f"pipeline {s.pipeline} not active after {wait_s} s (AWS: creation takes 5 to 10 minutes)")


def update_item(table, changes: dict) -> None:
    table.update_item(Key={"id": CHANGED}, UpdateExpression="SET " + ", ".join(f"#{k} = :{k}" for k in changes),
                      ExpressionAttributeNames={f"#{k}": k for k in changes},
                      ExpressionAttributeValues={f":{k}": v for k, v in changes.items()})


def run_local(s: Settings) -> str:
    from moto import mock_aws

    from main.infra import create_table
    from standin_pipeline import StandInIndex, StandInPipeline

    lines = []
    errors = validate_index(s.index, index_body(s))
    es, ex = example_settings()
    errors += validate(render(es, **ex), es)
    lines.append(f"mappings and pipeline definition: {'valid' if not errors else errors}")
    offline_env(s.region)
    with mock_aws():
        ddb = boto3.client("dynamodb", region_name=s.region)
        create_table(ddb, s, load_items())
        index = StandInIndex(s.index, index_body(s))
        pipe = StandInPipeline(ddb, boto3.client("dynamodbstreams", region_name=s.region), s.table, index,
                               index_version=s.index_version)
        exported = pipe.export(export_version=int(time.time() * 1000))
        lines.append(f"initial export: {exported} items -> {len(index.docs)} documents in {s.index}")
        lines.append(f"search {QUERY!r} before the update: {index.search(QUERY)}")
        table = boto3.resource("dynamodb", region_name=s.region).Table(s.table)
        update_item(table, CHANGE)
        table.delete_item(Key={"id": "I08"})
        changes = pipe.poll()
        doc = index.get(CHANGED)
        lines.append(f"stream: {changes} changes applied; {CHANGED} in the index now: {doc['name']!r}, "
                     f"in_stock={doc['in_stock']}, index_version={doc['index_version']}")
        lines.append(f"I08 removed from the table, in the index: {index.get('I08') is not None}")
        lines.append(f"search {QUERY!r} after the update: {index.search(QUERY)}")
        assert doc["name"] == CHANGE["name"] and index.get("I08") is None
    return "[local: moto DynamoDB and Streams, stand-in pipeline and index]\n" + "\n".join(lines)


def run_aws(s: Settings, wait_s: int = 600) -> str:
    from main.infra import (ROLE_SUFFIXES, account_id, caller_principal, client_for, create_collection,
                            create_index, create_table, ensure_role)

    c = lambda svc: boto3.client(svc, region_name=s.region)  # noqa: E731
    account, items = account_id(s.region), load_items()
    ddb = c("dynamodb")
    # The initial export reads from point-in-time recovery, so it must be on (AWS docs, pipeline prerequisites):
    # create_table turns it on, with the recipe's 7 days (main/infra.py enable_pitr).
    create_table(ddb, s, items)
    table_arn = ddb.describe_table(TableName=s.table)["Table"]["TableArn"]
    bucket = s.bucket(account)
    create_bucket(c("s3"), bucket, s.region)
    role = ensure_role(c("iam"), f"{s.prefix}{ROLE_SUFFIXES[2]}", "osis-pipelines.amazonaws.com",
                       pipeline_statements(s, account, table_arn, bucket), s.pipeline_role_arn)
    s = dataclasses.replace(s, pipeline_role_arn=role)
    # The pipeline also reads the index's mapping (DescribeIndex): it is a reader as well as a writer.
    endpoint = create_collection(c("opensearchserverless"), s, s.collection, "SEARCH", writers=[role],
                                 readers=[role, caller_principal(s.region)])
    client = client_for(endpoint, s.region)
    create_index(client, s.index, index_body(s))
    create_pipeline(c("osis"), s, render(s, table_arn, endpoint, bucket))
    deadline = time.time() + wait_s
    while client.count(index=s.index)["count"] < len(items):  # the export takes a few minutes
        if time.time() > deadline:
            raise TimeoutError("initial export not in the index yet; see the pipeline's logs and $PREFIX/osis-dlq/")
        time.sleep(15)
    table = boto3.resource("dynamodb", region_name=s.region).Table(s.table)
    update_item(table, CHANGE)
    changed_at = time.time()
    while True:
        hits = client.search(index=s.index, body={"query": {"ids": {"values": [CHANGED]}}})["hits"]["hits"]
        if hits and hits[0]["_source"].get("name") == CHANGE["name"]:
            break
        if time.time() > deadline + wait_s:
            raise TimeoutError(f"the update to {CHANGED} has not reached the index")
        time.sleep(5)
    found = [h["_id"] for h in client.search(index=s.index, body=keyword_query(QUERY))["hits"]["hits"]]
    return (f"[aws {s.region}; pipeline {s.pipeline}]\nupdate to {CHANGED} searchable after about "
            f"{time.time() - changed_at:.0f} s (polled every 5 s)\nsearch {QUERY!r}: {found}")


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--target", choices=["local", "aws"], default="local")
    args = p.parse_args(argv)
    s = Settings()
    print(run_aws(s) if args.target == "aws" else run_local(s))


if __name__ == "__main__":
    main()

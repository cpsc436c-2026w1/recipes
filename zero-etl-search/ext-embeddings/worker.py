"""Re-embedding worker: read the queue, re-read the item from DynamoDB, embed it, write the vector index.

  uv run python ext-embeddings/worker.py                   offline: moto table and queues, stub embedder
  uv run python ext-embeddings/worker.py --setup --target aws   create queues, the enqueue function, vector index
  uv run python ext-embeddings/worker.py --target aws      drain the queue once with the course LLM gateway

The worker runs where you run it (laptop or CloudShell), so your course MCP token stays in your shell and is never
stored in AWS. A team service would run it as a Lambda function on the queue with the token in Secrets Manager.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from embed import GatewayEmbedder, StubEmbedder  # noqa: E402
from enqueue import embed_text, text_hash  # noqa: E402

from main.config import Settings  # noqa: E402

MAX_RECEIVES = 3          # ours: a message that fails three times moves to the dead-letter queue
VISIBILITY_S = 120        # ours: longer than one embedding call with retries, so two workers do not take one message
VECTOR_INDEX_VERSION = 1


def queue_names(s: Settings) -> tuple[str, str]:
    return f"{s.prefix}-reembed", f"{s.prefix}-reembed-dlq"


def vector_index(s: Settings) -> str:
    return f"item-vectors-v{VECTOR_INDEX_VERSION}"


def vector_index_body(s: Settings, dim: int, model: str) -> dict:
    return {
        "settings": {"index": {"knn": True}},
        "mappings": {
            "dynamic": False,
            # The model is part of the version: vectors from two models are not comparable, so a model change
            # means a new index (item-vectors-v2), filled beside the old one.
            "_meta": {"index_version": VECTOR_INDEX_VERSION, "embedding_model": model, "dimension": dim,
                      "source_table": s.table},
            "properties": {
                "embedding": {"type": "knn_vector", "dimension": dim, "space_type": "cosinesimil"},
                "text_hash": {"type": "keyword"},
                "embedding_model": {"type": "keyword"},
            },
        },
    }


def _create_queue(sqs, sleep, **kw) -> str:
    """create_queue, waiting when clean_up.py deleted a queue of this name less than a minute ago: SQS refuses the
    name for 60 s after a delete (AWS docs, SQS DeleteQueue). Waits 10 s at a time, at most 70 s."""
    for attempt in range(8):
        try:
            return sqs.create_queue(**kw)["QueueUrl"]
        except sqs.exceptions.QueueDeletedRecently:
            if attempt == 7:
                raise
            sleep(10)
    raise AssertionError("unreachable")  # the loop returns or raises


def create_queues(sqs, s: Settings, visibility_s: int = VISIBILITY_S, sleep=time.sleep) -> tuple[str, str]:
    main_name, dlq_name = queue_names(s)
    dlq_url = _create_queue(sqs, sleep, QueueName=dlq_name, Attributes={"MessageRetentionPeriod": "1209600"})
    dlq_arn = sqs.get_queue_attributes(QueueUrl=dlq_url, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
    url = _create_queue(sqs, sleep, QueueName=main_name, Attributes={
        "VisibilityTimeout": str(visibility_s),
        "RedrivePolicy": json.dumps({"deadLetterTargetArn": dlq_arn, "maxReceiveCount": str(MAX_RECEIVES)}),
    })
    return url, dlq_url


class StandInVectors:
    def __init__(self):
        self.docs: dict[str, dict] = {}

    def get(self, doc_id):
        return self.docs.get(doc_id)

    def put(self, doc_id, doc):
        self.docs[doc_id] = doc

    def delete(self, doc_id):
        self.docs.pop(doc_id, None)


class OpenSearchVectors:
    def __init__(self, client, index: str):
        self.client, self.index = client, index

    def get(self, doc_id):
        r = self.client.get(index=self.index, id=doc_id, ignore=[404])
        return r["_source"] if r.get("found") else None

    def put(self, doc_id, doc):
        self.client.index(index=self.index, id=doc_id, body=doc)

    def delete(self, doc_id):
        self.client.delete(index=self.index, id=doc_id, ignore=[404])


def handle(msg: dict, table, embedder, store) -> str:
    """One message. Returns what happened; raises if the item could not be embedded (the message then stays)."""
    if msg["op"] == "REMOVE":
        store.delete(msg["id"])
        return "removed"
    # DynamoDB is the source of truth: read the item now, not the text at the time of the change.
    item = table.get_item(Key={"id": msg["id"]}, ConsistentRead=True).get("Item")
    if item is None:
        store.delete(msg["id"])
        return "removed"
    h = text_hash(item)
    if h != msg["text_hash"]:
        return "stale"  # the text changed again; a newer message carries the new hash
    existing = store.get(msg["id"])
    if existing and existing["text_hash"] == h and existing["embedding_model"] == embedder.model:
        return "already"  # a repeated message: the vector is current, no model call
    vector = embedder.embed([embed_text(item)])[0]
    store.put(msg["id"], {"embedding": vector, "text_hash": h, "embedding_model": embedder.model})
    return "embedded"


def drain(sqs, queue_url: str, table, embedder, store, wait_s: int = 0, rounds: int = 20, log=None) -> dict:
    """Handle what is in the queue; return how many messages ended each way. A message that fails stays in the
    queue, and the reason of each failure goes to `log` (stderr by default), once per distinct reason: a count of
    'failed' alone does not say whether the model was busy, the key was refused or the code is wrong."""
    log = log or (lambda line: print(line, file=sys.stderr))
    counts: dict[str, int] = {}
    reasons: set[str] = set()
    for _ in range(rounds):
        msgs = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=10, WaitTimeSeconds=wait_s).get(
            "Messages", [])
        if not msgs:
            break
        for m in msgs:
            try:
                outcome = handle(json.loads(m["Body"]), table, embedder, store)
                sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=m["ReceiptHandle"])
            except Exception as err:  # noqa: BLE001 - leave the message; SQS gives it again after the visibility timeout
                outcome = "failed"
                reason = f"{type(err).__name__}: {err}"
                if reason not in reasons:
                    reasons.add(reason)
                    log(f"failed, left in the queue: {reason}")
            counts[outcome] = counts.get(outcome, 0) + 1
    return counts


def queue_depth(sqs, url: str) -> int:
    return int(sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["ApproximateNumberOfMessages"])[
        "Attributes"]["ApproximateNumberOfMessages"])


# --- offline ------------------------------------------------------------------------------------------------

def run_local(s: Settings) -> str:
    import boto3
    from moto import mock_aws

    from enqueue import handler as enqueue
    from main.config import load_items, offline_env
    from main.infra import create_table

    offline_env(s.region)
    lines = []
    with mock_aws():
        ddb = boto3.client("dynamodb", region_name=s.region)
        create_table(ddb, s, load_items())
        sqs = boto3.client("sqs", region_name=s.region)
        url, dlq = create_queues(sqs, s, visibility_s=0)  # 0 offline so retries come at once
        streams = boto3.client("dynamodbstreams", region_name=s.region)
        arn = ddb.describe_table(TableName=s.table)["Table"]["LatestStreamArn"]
        shard = streams.describe_stream(StreamArn=arn)["StreamDescription"]["Shards"][0]["ShardId"]
        it = streams.get_shard_iterator(StreamArn=arn, ShardId=shard, ShardIteratorType="TRIM_HORIZON")[
            "ShardIterator"]
        table = boto3.resource("dynamodb", region_name=s.region).Table(s.table)
        embedder, store = StubEmbedder(fail_for={"Trekking"}), StandInVectors()

        def step(label):
            nonlocal it
            resp = streams.get_records(ShardIterator=it)
            it = resp["NextShardIterator"]
            q = enqueue({"Records": resp["Records"]}, None, sqs=sqs, queue_url=url)
            d = drain(sqs, url, table, embedder, store)
            lines.append(f"{label:<34} queued {q['queued']}, skipped {q['skipped']}; worker {d}")

        step("initial items")
        table.update_item(Key={"id": "I03"}, UpdateExpression="SET price_cad = :p",
                          ExpressionAttributeValues={":p": 29})
        step("price change on I03")
        table.update_item(Key={"id": "I05"}, UpdateExpression="SET description = :d",
                          ExpressionAttributeValues={":d": "Thin merino liner sock for warm days"})
        step("description change on I05")
        lines.append(f"vectors in the index: {len(store.docs)} of {len(load_items())} items; model calls "
                     f"{embedder.calls}")
        dead = sqs.receive_message(QueueUrl=dlq, MaxNumberOfMessages=10).get("Messages", [])
        lines.append(f"dead-letter queue: {[json.loads(m['Body'])['id'] for m in dead]} "
                     f"(the stub model fails on it; {MAX_RECEIVES} tries, then moved)")
    return "[local: moto DynamoDB, Streams and SQS; stub embedder]\n" + "\n".join(lines)


# --- AWS ----------------------------------------------------------------------------------------------------

def setup_aws(s: Settings) -> str:
    """Queues, the enqueue function on the table's stream, the vector collection and index."""
    import io
    import os
    import zipfile

    import boto3

    from main.infra import ROLE_SUFFIXES, caller_principal, client_for, create_collection, create_index, ensure_role

    c = lambda svc: boto3.client(svc, region_name=s.region)  # noqa: E731
    url, dlq_url = create_queues(c("sqs"), s)
    stream_arn = c("dynamodb").describe_table(TableName=s.table)["Table"]["LatestStreamArn"]
    queue_arn = c("sqs").get_queue_attributes(QueueUrl=url, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
    dlq_arn = c("sqs").get_queue_attributes(QueueUrl=dlq_url, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
    fn_name = f"{s.prefix}-reembed-enqueue"
    # The function's role: read this stream, send to the queue and (for a batch Lambda gives up on) the dead-letter
    # queue, write its own logs. Created unless LAMBDA_ROLE_ARN
    # names a role you already have.
    role = ensure_role(c("iam"), f"{s.prefix}{ROLE_SUFFIXES[1]}", "lambda.amazonaws.com", [
        {"Effect": "Allow", "Resource": stream_arn,
         "Action": ["dynamodb:GetRecords", "dynamodb:GetShardIterator", "dynamodb:DescribeStream",
                    "dynamodb:ListStreams"]},
        {"Effect": "Allow", "Action": ["sqs:SendMessage"], "Resource": [queue_arn, dlq_arn]},
        {"Effect": "Allow", "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
         "Resource": f"arn:aws:logs:{s.region}:*:log-group:/aws/lambda/{fn_name}:*"},
    ], os.environ.get("LAMBDA_ROLE_ARN", ""))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.write(Path(__file__).resolve().parent / "enqueue.py", "enqueue.py")
    lam, fn = c("lambda"), f"{s.prefix}-reembed-enqueue"
    try:
        lam.create_function(FunctionName=fn, Runtime="python3.12", Role=role, Handler="enqueue.handler",
                            Code={"ZipFile": buf.getvalue()}, Timeout=30,
                            Environment={"Variables": {"QUEUE_URL": url}}, Tags={"recipe": "zero-etl-search"})
        lam.get_waiter("function_active_v2").wait(FunctionName=fn)
    except lam.exceptions.ResourceConflictException:
        pass
    if not lam.list_event_source_mappings(FunctionName=fn, EventSourceArn=stream_arn)["EventSourceMappings"]:
        # The second reader of this stream (the indexing function is the first). AWS advises at most two readers
        # per shard at a time, so a third consumer would read from this queue, not from the stream.
        # A batch it gives up on (10 retries, or records older than an hour) goes to the dead-letter queue, so the
        # shard does not wait a day behind it (https://docs.aws.amazon.com/lambda/latest/dg/services-dynamodb-errors.html).
        # Those messages say where the records are on the stream, not what they are: main/envs.py record_keys().
        lam.create_event_source_mapping(FunctionName=fn, EventSourceArn=stream_arn, StartingPosition="TRIM_HORIZON",
                                        BatchSize=100, MaximumRetryAttempts=10, BisectBatchOnFunctionError=True,
                                        MaximumRecordAgeInSeconds=3600,
                                        DestinationConfig={"OnFailure": {"Destination": dlq_arn}})
    # You write the vectors yourself (this worker), so you are both the writer and the reader of this index.
    endpoint = create_collection(c("opensearchserverless"), s, s.vector_collection, "VECTORSEARCH",
                                 writers=[caller_principal(s.region)], index=vector_index(s),
                                 group=s.vector_group)
    meta = create_index(client_for(endpoint, s.region), vector_index(s),
                        vector_index_body(s, GatewayEmbedder.dim, GatewayEmbedder.model))
    return f"queue {url}\nfunction {fn} on {stream_arn}\nvector index {vector_index(s)}: {meta}\nendpoint {endpoint}"


def run_aws(s: Settings) -> str:
    import boto3

    from main.infra import client_for

    c = lambda svc: boto3.client(svc, region_name=s.region)  # noqa: E731
    sqs = c("sqs")
    url = sqs.get_queue_url(QueueName=queue_names(s)[0])["QueueUrl"]
    dlq = sqs.get_queue_url(QueueName=queue_names(s)[1])["QueueUrl"]
    endpoint = c("opensearchserverless").batch_get_collection(names=[s.vector_collection])[
        "collectionDetails"][0]["collectionEndpoint"]
    store = OpenSearchVectors(client_for(endpoint, s.region), vector_index(s))
    table = boto3.resource("dynamodb", region_name=s.region).Table(s.table)
    counts = drain(sqs, url, table, GatewayEmbedder(), store, wait_s=10)
    return f"worker: {counts}; still queued {queue_depth(sqs, url)}; dead letters {queue_depth(sqs, dlq)}"


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--target", choices=["local", "aws"], default="local")
    p.add_argument("--setup", action="store_true", help="create queues, function and vector index (aws)")
    args = p.parse_args(argv)
    s = Settings()
    if args.target == "aws":
        print(setup_aws(s) if args.setup else run_aws(s))
    else:
        print(run_local(s))


if __name__ == "__main__":
    main()

"""Where the scenario and the demo run: offline (moto table and stream, stand-in mapping and index) or AWS.

Both give the same few calls, so main/run.py and demo/search_demo.py do not care which one they have:

  table               the DynamoDB table (boto3 resource): the app writes here and only here
  deliver()           offline: hand what is new on the stream to the function; AWS: nothing, Lambda does it
  search(body)        hits from the index: [{"_id", "_score", "_source"}]
  count()             documents in the index
  replay(records)     call the function again with records it has already had (a retried batch)
  failures()          messages in the failure queue
  record_keys(msg)    the item keys of the batch a failure message points to (read back from the stream)
  live                True on AWS: a change takes real time to become searchable
"""

from __future__ import annotations

import json
import time

import boto3

from main.config import Settings, load_items, offline_env
from main.mappings import index_body


class LocalEnv:
    live = False

    def __init__(self, s: Settings, items: list[dict] | None = None):
        from moto import mock_aws

        from main import handler
        from main.infra import create_failure_queue, create_table, write_items
        from main.standin import StandInIndex, StandInMapping

        self.s = s
        offline_env(s.region)
        self._mock = mock_aws()
        self._mock.start()
        self.ddb = boto3.client("dynamodb", region_name=s.region)
        self.streams = boto3.client("dynamodbstreams", region_name=s.region)
        self.sqs = boto3.client("sqs", region_name=s.region)
        self.stream_arn = create_table(self.ddb, s)
        self.queue_url, _ = create_failure_queue(self.sqs, s)
        self.index = StandInIndex(s.index, index_body(s))
        self.log: list[str] = []     # what the function would write to CloudWatch Logs, one line per batch
        self.post = self.index.post   # tests swap in a stand-in that fails for a while
        self.function = lambda event, context: handler.apply(event["Records"], lambda p, b: self.post(p, b),
                                                            s.index, s.index_version, log=self.log.append,
                                                            sleep=lambda _: None)
        from main.infra import BATCH_SIZE, MAX_RETRIES
        self.mapping = StandInMapping(self.streams, self.stream_arn, self.function, self.sqs, self.queue_url,
                                      batch_size=BATCH_SIZE, max_retries=MAX_RETRIES)
        self.table = boto3.resource("dynamodb", region_name=s.region).Table(s.table)
        write_items(self.table, load_items() if items is None else items)

    def deliver(self) -> dict:
        return self.mapping.deliver()

    def pending(self) -> int:
        return self.mapping.pending()

    def search(self, body: dict) -> list[dict]:
        return self.index.search(body)

    def count(self) -> int:
        return self.index.count()

    def mapping_meta(self) -> dict:
        return self.index.mapping_meta()

    def replay(self, records: list[dict]) -> str:
        try:
            resp = self.function({"Records": records}, None)
        except Exception as err:  # noqa: BLE001 - reported as Lambda reports a function error
            return f"function error: {err}"
        return "ok" if not resp.get("batchItemFailures") else f"not written from {resp['batchItemFailures']}"

    def failures(self, wait_s: int = 0) -> list[dict]:
        msgs = self.sqs.receive_message(QueueUrl=self.queue_url, MaxNumberOfMessages=10).get("Messages", [])
        return [json.loads(m["Body"]) for m in msgs]

    def record_keys(self, msg: dict) -> list[str]:
        return record_keys(self.streams, msg)

    def close(self) -> None:
        self._mock.stop()


class AwsEnv:
    live = True

    def __init__(self, s: Settings, info: dict | None = None):
        """`info` from main.infra.create_all(); without it, connect to what an earlier run created."""
        from main.infra import client_for

        self.s = s
        c = lambda svc: boto3.client(svc, region_name=s.region)  # noqa: E731
        self.lam, self.sqs, self.streams = c("lambda"), c("sqs"), c("dynamodbstreams")
        if info is None:
            found = c("opensearchserverless").batch_get_collection(names=[s.collection])["collectionDetails"]
            if not found:
                raise SystemExit(f"no collection {s.collection}: run `uv run python -m main.run --target aws` first")
            endpoint = found[0]["collectionEndpoint"]
            info = {"client": client_for(endpoint, s.region),
                    "queue_url": self.sqs.get_queue_url(QueueName=s.failure_queue)["QueueUrl"]}
        self.client, self.queue_url = info["client"], info["queue_url"]
        self.table = boto3.resource("dynamodb", region_name=s.region).Table(s.table)

    def deliver(self) -> dict:
        return {}

    def pending(self) -> int:
        return 0

    def search(self, body: dict) -> list[dict]:
        return self.client.search(index=self.s.index, body=body)["hits"]["hits"]

    def count(self) -> int:
        return self.client.count(index=self.s.index)["count"]

    def mapping_meta(self) -> dict:
        return self.client.indices.get_mapping(index=self.s.index)[self.s.index]["mappings"]["_meta"]

    def replay(self, records: list[dict]) -> str:
        """Invoke the deployed function with the same records it had already had, as Lambda does on a retry."""
        resp = self.lam.invoke(FunctionName=self.s.function, Payload=json.dumps({"Records": records}).encode())
        payload = resp["Payload"].read().decode()
        if resp.get("FunctionError"):
            return f"function error: {payload[:300]}"
        return "ok" if not json.loads(payload or "{}").get("batchItemFailures") else f"not written: {payload[:300]}"

    def failures(self, wait_s: int = 0) -> list[dict]:
        msgs = self.sqs.receive_message(QueueUrl=self.queue_url, MaxNumberOfMessages=10,
                                        WaitTimeSeconds=wait_s).get("Messages", [])
        return [json.loads(m["Body"]) for m in msgs]

    def record_keys(self, msg: dict) -> list[str]:
        return record_keys(self.streams, msg)

    def close(self) -> None:
        pass


def record_keys(streams, msg: dict) -> list[str]:
    """The failure message says where the records are (shard, first sequence number, batch size), not what they
    are: read them back from the stream, which keeps them 24 hours."""
    info = msg["DDBStreamBatchInfo"]
    it = streams.get_shard_iterator(StreamArn=info["streamArn"], ShardId=info["shardId"],
                                    ShardIteratorType="AT_SEQUENCE_NUMBER",
                                    SequenceNumber=info["startSequenceNumber"])["ShardIterator"]
    records = streams.get_records(ShardIterator=it, Limit=info["batchSize"])["Records"]
    return [r["dynamodb"]["Keys"]["id"]["S"] for r in records]


def stream_records(items: list[dict], event: str = "INSERT") -> list[dict]:
    """Stream-shaped records for items as they are now, in the form Lambda passes them to the function."""
    from boto3.dynamodb.types import TypeSerializer

    from main.infra import _decimals

    ser = TypeSerializer()
    out = []
    for n, it in enumerate(items, 1):
        image = {k: ser.serialize(v) for k, v in _decimals(it).items()}
        out.append({"eventName": event, "eventSource": "aws:dynamodb", "dynamodb": {
            "Keys": {"id": image["id"]}, "NewImage": image, "SequenceNumber": str(n),
            "ApproximateCreationDateTime": int(time.time())}})
    return out

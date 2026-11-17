"""Offline stand-ins for the two AWS parts that moto does not run: the OpenSearch index and Lambda's event source
mapping. They keep the rules this recipe depends on, so the offline run and the tests show the same behaviour as
AWS, and the real handler (main/handler.py) runs unchanged against them.

StandInIndex    a _bulk endpoint with document ids (the same id twice = one document), mapped field types (a value
                the mapping cannot take is refused, as OpenSearch refuses it with mapper_parsing_exception;
                unmapped attributes stay in _source but are not searchable: `dynamic: false`), and BM25 scoring
                of a multi_match query (k1 = 1.2, b = 0.75, Lucene's defaults). It has no refresh delay: offline,
                a change is searchable as soon as it is delivered.
StandInMapping  reads the moto table's stream and calls the function with batches, as Lambda does: BatchSize,
                BisectBatchOnFunctionError (a failed batch is split in two; splitting does not use up retries),
                MaximumRetryAttempts, partial batch responses (ReportBatchItemFailures), then a message to the
                on-failure queue and on to the next records (AWS docs, "Retain discarded records for a DynamoDB
                event source in Lambda" and "Configuring partial batch response with DynamoDB and Lambda").
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone

K1, B = 1.2, 0.75


def _words(text) -> list[str]:
    return re.findall(r"\w+", str(text).lower())


def _fits(value, spec: dict) -> bool:
    kind = spec["type"]
    if value is None:
        return True
    if kind in ("float", "double", "integer", "long"):
        if isinstance(value, bool):
            return False
        if isinstance(value, (int, float)):
            return True
        try:
            float(value)              # OpenSearch takes a number written as a string ("12.5"), not "free"
            return True
        except (TypeError, ValueError):
            return False
    if kind == "boolean":
        return isinstance(value, bool) or value in ("true", "false")
    return isinstance(value, (str, int, float))


class StandInIndex:
    def __init__(self, name: str, body: dict):
        self.name, self.body = name, body
        self.properties = body["mappings"]["properties"]
        self.docs: dict[str, dict] = {}
        self.requests = 0

    # --- what the function calls ---------------------------------------------------------------------------
    def post(self, path: str, body: str) -> dict:
        """The _bulk API, in the shape OpenSearch answers it."""
        assert path == "/_bulk", path
        self.requests += 1
        lines = [json.loads(line) for line in body.splitlines() if line.strip()]
        items, i = [], 0
        while i < len(lines):
            (action, meta), = lines[i].items()
            doc_id = meta["_id"]
            if action == "delete":
                found = self.docs.pop(doc_id, None) is not None
                items.append({"delete": {"_id": doc_id, "status": 200 if found else 404,
                                         "result": "deleted" if found else "not_found"}})
                i += 1
                continue
            source = lines[i + 1]
            bad = [f for f, spec in self.properties.items() if f in source and not _fits(source[f], spec)]
            if bad:
                items.append({action: {"_id": doc_id, "status": 400, "error": {
                    "type": "mapper_parsing_exception",
                    "reason": f"failed to parse field [{bad[0]}] of type [{self.properties[bad[0]]['type']}]"}}})
            else:
                created = doc_id not in self.docs
                self.docs[doc_id] = source
                items.append({action: {"_id": doc_id, "status": 201 if created else 200,
                                       "result": "created" if created else "updated"}})
            i += 2
        return {"errors": any(next(iter(it.values()))["status"] >= 300 for it in items), "items": items}

    # --- what the app and the demo call --------------------------------------------------------------------
    def get(self, doc_id: str) -> dict | None:
        return self.docs.get(doc_id)

    def count(self) -> int:
        return len(self.docs)

    def mapping_meta(self) -> dict:
        return self.body["mappings"]["_meta"]

    def search(self, query: dict) -> list[dict]:
        """Hits for main.mappings.keyword_query() (multi_match, best_fields: a document's score is its best
        field's BM25 score times that field's boost) or an `ids` query; [{"_id", "_score", "_source"}]."""
        q = query["query"]
        if "ids" in q:
            return [{"_id": i, "_score": 1.0, "_source": self.docs[i]} for i in q["ids"]["values"] if i in self.docs]
        mm = q.get("multi_match") or q["bool"]["must"][0]["multi_match"]
        terms = _words(mm["query"])
        hits = []
        for doc_id, src in self.docs.items():
            best = 0.0
            for f in mm["fields"]:
                name, _, boost = f.partition("^")
                spec = self.properties.get(name)
                if spec is None or name not in src:
                    continue
                if spec["type"] == "keyword":   # a keyword field matches the whole query string, as written
                    score = 1.0 if str(src[name]) == mm["query"] else 0.0
                else:
                    score = self._bm25(name, terms, src)
                best = max(best, score * float(boost or 1))
            if best > 0:
                hits.append({"_id": doc_id, "_score": round(best, 4), "_source": src})
        hits.sort(key=lambda h: (-h["_score"], h["_id"]))
        return hits[: query.get("size", 10)]

    def _bm25(self, field: str, terms: list[str], src: dict) -> float:
        docs = [_words(d.get(field, "")) for d in self.docs.values() if field in d]
        n, avg = len(docs), (sum(map(len, docs)) / len(docs)) if docs else 1
        words = _words(src[field])
        score = 0.0
        for t in set(terms):
            df = sum(t in d for d in docs)
            tf = words.count(t)
            if not tf:
                continue
            idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
            score += idf * tf * (K1 + 1) / (tf + K1 * (1 - B + B * len(words) / avg))
        return score


def _lambda_shape(record: dict) -> dict:
    """A record from GetRecords, in the shape Lambda passes it: the stream time as epoch seconds."""
    d = dict(record["dynamodb"])
    t = d.get("ApproximateCreationDateTime")
    if isinstance(t, datetime):
        d["ApproximateCreationDateTime"] = int(t.astimezone(timezone.utc).timestamp())
    return {**record, "dynamodb": d}


class StandInMapping:
    """Lambda's DynamoDB event source mapping, offline: deliver() calls `function` with what is new on the stream."""

    def __init__(self, streams, stream_arn: str, function, sqs=None, failure_queue_url: str = "",
                 batch_size: int = 10, max_retries: int = 2, bisect: bool = True, starting: str = "TRIM_HORIZON"):
        self.streams, self.stream_arn, self.function = streams, stream_arn, function
        self.sqs, self.failure_queue_url = sqs, failure_queue_url
        self.batch_size, self.max_retries, self.bisect = batch_size, max_retries, bisect
        self.invocations = 0
        self.discarded_keys: list[str] = []
        self.iterators = {
            sh["ShardId"]: streams.get_shard_iterator(StreamArn=stream_arn, ShardId=sh["ShardId"],
                                                      ShardIteratorType=starting)["ShardIterator"]
            for sh in streams.describe_stream(StreamArn=stream_arn)["StreamDescription"]["Shards"]}

    def pending(self) -> int:
        """How many records are on the stream and not yet delivered (reads them without moving on)."""
        return sum(len(self.streams.get_records(ShardIterator=it)["Records"]) for it in self.iterators.values())

    def deliver(self) -> dict:
        counts = {"records": 0, "batches": 0, "discarded": 0}
        for shard, it in list(self.iterators.items()):
            resp = self.streams.get_records(ShardIterator=it)
            self.iterators[shard] = resp["NextShardIterator"]
            records = [_lambda_shape(r) for r in resp["Records"]]
            for start in range(0, len(records), self.batch_size):
                batch = records[start:start + self.batch_size]
                counts["records"] += len(batch)
                counts["batches"] += 1
                counts["discarded"] += self._run(batch, shard, self.max_retries)
        return counts

    def _run(self, batch: list[dict], shard: str, retries_left: int) -> int:
        """One invocation and what Lambda does with its outcome. Returns how many records were discarded.

        Function error: split the batch in two (no retry used up), or retry, then discard to the failure queue.
        Partial batch response (ReportBatchItemFailures): the records before the lowest reported sequence number
        are done; the rest is tried again (one retry used), then discarded.
        (https://docs.aws.amazon.com/lambda/latest/dg/services-ddb-batchfailurereporting.html)"""
        self.invocations += 1
        try:
            resp = self.function({"Records": batch}, None) or {}
        except Exception:  # noqa: BLE001 - any function error is a failed batch, as in Lambda
            if self.bisect and len(batch) > 1:
                half = len(batch) // 2
                return self._run(batch[:half], shard, retries_left) + self._run(batch[half:], shard, retries_left)
            if retries_left > 0:
                return self._run(batch, shard, retries_left - 1)
            return self._discard(batch, shard)
        failed = [f["itemIdentifier"] for f in resp.get("batchItemFailures") or []]
        if not failed:
            return 0
        seqs = [r["dynamodb"]["SequenceNumber"] for r in batch]
        rest = batch[min(seqs.index(f) for f in failed):]
        if retries_left > 0:
            return self._run(rest, shard, retries_left - 1)
        return self._discard(rest, shard)

    def _discard(self, batch: list[dict], shard: str) -> int:
        if self.sqs and self.failure_queue_url:
            self.sqs.send_message(QueueUrl=self.failure_queue_url, MessageBody=json.dumps(failure_message(
                batch, shard, self.stream_arn, self.max_retries + 1)))
        self.discarded_keys += [r["dynamodb"]["Keys"]["id"]["S"] for r in batch]
        return len(batch)


def failure_message(batch: list[dict], shard: str, stream_arn: str, invokes: int) -> dict:
    """What Lambda sends to an on-failure destination for a stream batch: where the records are, not the records
    (AWS docs, "Retain discarded records for a DynamoDB event source in Lambda")."""
    first, last = batch[0]["dynamodb"], batch[-1]["dynamodb"]
    return {
        "requestContext": {"condition": "RetryAttemptsExhausted", "approximateInvokeCount": invokes},
        "responseContext": {"statusCode": 200, "functionError": "Unhandled"},
        "version": "1.0", "timestamp": datetime.now(timezone.utc).isoformat(),
        "DDBStreamBatchInfo": {
            "shardId": shard, "streamArn": stream_arn, "batchSize": len(batch),
            "startSequenceNumber": first["SequenceNumber"], "endSequenceNumber": last["SequenceNumber"],
            "approximateArrivalOfFirstRecord": first.get("ApproximateCreationDateTime"),
            "approximateArrivalOfLastRecord": last.get("ApproximateCreationDateTime"),
        },
    }

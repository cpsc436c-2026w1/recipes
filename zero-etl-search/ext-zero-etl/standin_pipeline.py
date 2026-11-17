"""ext-zero-etl offline stand-ins: an index that keeps the mapping rules that matter here, and a pipeline that reads a moto table.

StandInIndex   documents by id with external versions (an older version is refused, as OpenSearch refuses it
               with version_type=external), `dynamic: false` (unmapped attributes are kept in _source but not
               searchable), and a simple keyword match. Ranking is not BM25: see recipes/hybrid-retrieval-eval.
StandInPipeline  the two phases of the DynamoDB source: an export (here a Scan of the moto table) and then the
               stream (moto's DynamoDB Streams), turned into index and delete actions with a version each.
"""

from __future__ import annotations

import re
from decimal import Decimal

from boto3.dynamodb.types import TypeDeserializer

from main.mappings import TEXT_FIELDS

_deser = TypeDeserializer()


class VersionConflict(Exception):
    pass


def _plain(v):
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_plain(x) for x in v]
    return v


class StandInIndex:
    def __init__(self, name: str, body: dict):
        self.name, self.body = name, body
        self.properties = body["mappings"]["properties"]
        self.docs: dict[str, dict] = {}
        self.versions: dict[str, int] = {}  # kept after a delete too, as OpenSearch keeps a tombstone for a while

    def _check_version(self, doc_id: str, version: int) -> None:
        if doc_id in self.versions and version <= self.versions[doc_id]:
            raise VersionConflict(f"{doc_id}: version {version} is not above {self.versions[doc_id]}")

    def index(self, doc_id: str, source: dict, version: int) -> None:
        self._check_version(doc_id, version)
        self.docs[doc_id] = source
        self.versions[doc_id] = version

    def delete(self, doc_id: str, version: int) -> None:
        self._check_version(doc_id, version)
        self.docs.pop(doc_id, None)
        self.versions[doc_id] = version

    def get(self, doc_id: str) -> dict | None:
        return self.docs.get(doc_id)

    def mapping_meta(self) -> dict:
        return self.body["mappings"]["_meta"]

    def search(self, text: str, k: int = 5) -> list[str]:
        """Ids of documents whose mapped text fields share a word with the query, most shared words first."""
        words = set(re.findall(r"\w+", text.lower()))
        scored = []
        for doc_id, src in self.docs.items():
            hay = " ".join(str(src.get(f, "")) for f in TEXT_FIELDS if f in self.properties).lower()
            hits = len(words & set(re.findall(r"\w+", hay)))
            if hits:
                scored.append((-hits, doc_id))
        return [d for _, d in sorted(scored)[:k]]


class StandInPipeline:
    """Export first, then the stream, into one index: what the DynamoDB source and the opensearch sink do."""

    def __init__(self, ddb_client, streams_client, table: str, index: StandInIndex, key: str = "id",
                 index_version: int = 1):
        self.ddb, self.streams, self.table, self.index, self.key = ddb_client, streams_client, table, index, key
        self.index_version = index_version
        self.iterators: dict[str, str] = {}
        self.refused = 0  # changes older than the indexed document (version conflicts), skipped as the sink does

    def _source(self, image: dict) -> dict:
        item = {k: _plain(_deser.deserialize(v)) for k, v in image.items()}
        return {**item, "index_version": self.index_version}  # the add_entries processor in ext-zero-etl/pipeline.py

    def _apply(self, action: str, doc_id: str, source: dict | None, version: int) -> None:
        try:
            if action == "delete":
                self.index.delete(doc_id, version)
            else:
                self.index.index(doc_id, source, version)
        except VersionConflict:
            self.refused += 1

    def export(self, export_version: int) -> int:
        """Initial copy of the whole table; every document gets the export's version."""
        n = 0
        for page in self.ddb.get_paginator("scan").paginate(TableName=self.table):
            for image in page["Items"]:
                self._apply("index", image[self.key]["S"], self._source(image), export_version)
                n += 1
        self._start_stream_at_latest()
        return n

    def _start_stream_at_latest(self) -> None:
        arn = self.ddb.describe_table(TableName=self.table)["Table"]["LatestStreamArn"]
        self.stream_arn = arn
        for sh in self.streams.describe_stream(StreamArn=arn)["StreamDescription"]["Shards"]:
            self.iterators[sh["ShardId"]] = self.streams.get_shard_iterator(
                StreamArn=arn, ShardId=sh["ShardId"], ShardIteratorType="LATEST")["ShardIterator"]

    def poll(self) -> int:
        """Apply new stream records. The version is the record's sequence number: it grows with each change."""
        n = 0
        for shard, it in list(self.iterators.items()):
            resp = self.streams.get_records(ShardIterator=it)
            self.iterators[shard] = resp["NextShardIterator"]
            for r in resp["Records"]:
                d = r["dynamodb"]
                doc_id = d["Keys"][self.key]["S"]
                version = int(d["SequenceNumber"])
                if r["eventName"] == "REMOVE":
                    self._apply("delete", doc_id, None, version)
                else:
                    self._apply("index", doc_id, self._source(d["NewImage"]), version)
                n += 1
        return n

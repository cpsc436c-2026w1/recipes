"""Offline stand-ins for the lake side: a real Iceberg table on local disk, a Firehose that buffers, DuckDB for Athena.

- The Iceberg table is real: PyIceberg writes Parquet data files and Iceberg metadata, with a SQLite catalog in
  place of the Glue Data Catalog. Snapshots, time travel, rollback and snapshot expiry behave as in the lake.
  On AWS the same PyIceberg calls work against the Glue Data Catalog (`catalog("glue")`, used by the extensions).
- LocalFirehose keeps Firehose's one visible behaviour that matters here: records wait in a buffer, and each
  buffer flush is one commit (one snapshot, one or more new data files).
- DuckDB runs the same SQL text as Athena (main/sql/*.sql) over the table's current or an earlier snapshot.
  "Data scanned" here is the size of the data files the scan plans to read: Athena bills what it reads from S3,
  and file pruning works the same way, but the numbers are not Athena's.
"""

from __future__ import annotations

import datetime as dt
import json
import time
from pathlib import Path

import duckdb
import pyarrow as pa

from main.config import Settings, sql
from main.handler import ITEM_FIELDS, KEY

NAMESPACE = "cdc"
ARROW_TYPES = {"string": pa.string(), "double": pa.float64()}


def arrow_schema() -> pa.Schema:
    fields = [pa.field(KEY, pa.string()), pa.field("op", pa.string()),
              pa.field("change_time", pa.timestamp("us")), pa.field("arrival_time", pa.timestamp("us")),
              pa.field("seq", pa.string())]
    fields += [pa.field(n, ARROW_TYPES[t]) for n, t in ITEM_FIELDS.items()]
    return pa.schema(fields)


def iceberg_schema():
    """The Iceberg schema, from the same columns as the Athena DDL (tests/test_schema.py compares them)."""
    from pyiceberg.schema import Schema
    from pyiceberg.types import DoubleType, NestedField, StringType, TimestampType

    kinds = {"string": StringType(), "double": DoubleType(), "timestamp": TimestampType()}
    cols = [(KEY, "string"), ("op", "string"), ("change_time", "timestamp"), ("arrival_time", "timestamp"),
            ("seq", "string")] + list(ITEM_FIELDS.items())
    return Schema(*[NestedField(i + 1, n, kinds[t], required=False) for i, (n, t) in enumerate(cols)])


def partition_spec():
    from pyiceberg.partitioning import PartitionField, PartitionSpec
    from pyiceberg.transforms import DayTransform

    return PartitionSpec(PartitionField(source_id=3, field_id=1000, transform=DayTransform(), name="change_time_day"))


def local_catalog(root: Path):
    from pyiceberg.catalog.sql import SqlCatalog

    root.mkdir(parents=True, exist_ok=True)
    return SqlCatalog("local", uri=f"sqlite:///{root}/catalog.db", warehouse=f"file://{root}/warehouse")


def rows_to_arrow(rows: list[dict], schema: pa.Schema | None = None) -> pa.Table:
    schema = schema or arrow_schema()
    cols = {}
    for f in schema:
        vals = [r.get(f.name) for r in rows]
        if pa.types.is_timestamp(f.type):  # rows carry epoch microseconds, as Firehose wants them
            vals = [None if v is None else dt.datetime.fromtimestamp(v / 1e6, dt.UTC).replace(tzinfo=None)
                    for v in vals]
        cols[f.name] = pa.array(vals, type=f.type)
    return pa.table(cols, schema=schema)


class LocalLake:
    def __init__(self, root: Path, s: Settings | None = None):
        self.s = s or Settings()
        self.catalog = local_catalog(root)
        self.catalog.create_namespace_if_not_exists(NAMESPACE)
        ident = f"{NAMESPACE}.{self.s.lake_table}"
        if self.catalog.table_exists(ident):
            self.table = self.catalog.load_table(ident)
        else:
            self.table = self.catalog.create_table(ident, schema=iceberg_schema(), partition_spec=partition_spec())

    def append(self, rows: list[dict]) -> int:
        """One commit; returns the new snapshot id."""
        self.table.append(rows_to_arrow(rows, self.table.schema().as_arrow()))
        return self.table.current_snapshot().snapshot_id

    def arrow(self, snapshot_id: int | None = None) -> pa.Table:
        return self.table.scan(snapshot_id=snapshot_id).to_arrow()

    def query(self, text: str, snapshot_id: int | None = None) -> list[dict]:
        """Run Athena-style SQL in DuckDB over the table as of a snapshot (default: the current one)."""
        con = duckdb.connect()
        con.register(self.s.lake_table, self.arrow(snapshot_id))
        con.execute(sql("current_view", view=self.s.view, table=self.s.lake_table))
        cur = con.execute(text)
        names = [d[0] for d in cur.description]
        return [dict(zip(names, r)) for r in cur.fetchall()]

    def data_scanned(self, row_filter: str | None = None, snapshot_id: int | None = None) -> int:
        """Bytes of the data files a scan would read: Iceberg prunes files by partition and column statistics."""
        scan = self.table.scan(row_filter=row_filter, snapshot_id=snapshot_id) if row_filter else \
            self.table.scan(snapshot_id=snapshot_id)
        return sum(t.file.file_size_in_bytes for t in scan.plan_files())

    def data_files(self) -> int:
        return len(list(self.table.scan().plan_files()))


class LocalFirehose:
    """Stand-in for a Firehose stream with an Iceberg destination: buffer, then one commit per flush."""

    def __init__(self, lake: LocalLake, interval_s: float, clock=time.time, fail_next: int = 0):
        self.lake, self.interval_s, self.clock = lake, interval_s, clock
        self.buffer: list[dict] = []
        self.first_at: float | None = None
        self.fail_next = fail_next  # tests: reject this many records, as a throttled Firehose would
        self.flushes: list[float] = []

    def put_record_batch(self, DeliveryStreamName: str, Records: list[dict]) -> dict:
        responses, failed = [], 0
        for r in Records:
            if self.fail_next > 0:
                self.fail_next -= 1
                failed += 1
                responses.append({"ErrorCode": "ServiceUnavailableException", "ErrorMessage": "slow down"})
                continue
            self.buffer.append(json.loads(r["Data"]))
            self.first_at = self.first_at or self.clock()
            responses.append({"RecordId": str(len(self.buffer))})
        return {"FailedPutCount": failed, "RequestResponses": responses}

    def tick(self) -> bool:
        """Flush if the oldest buffered record has waited the buffer interval. Returns True if it flushed."""
        if self.buffer and self.clock() - self.first_at >= self.interval_s:
            self.flush()
            return True
        return False

    def flush(self) -> None:
        if self.buffer:
            self.lake.append(self.buffer)
            self.flushes.append(self.clock())
        self.buffer, self.first_at = [], None

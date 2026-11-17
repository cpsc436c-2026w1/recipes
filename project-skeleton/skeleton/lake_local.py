"""The offline stand-in for S3 + Athena: files in a local folder, queried with DuckDB.

The folder layout copies what AWS writes, so the same SQL runs on both:
- lake/updates/dt=YYYY-MM-DD/*.parquet          what Firehose writes (rep-lakehouse-first)
- lake/exports/AWSDynamoDB/<id>/data/*.json.gz  what a DynamoDB export writes (rep-operational-first)
"""
import gzip
import json
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from skeleton.events import parse
from skeleton.number import EVENTS_VIEW_SQL

LOCAL_ROOT = Path(".local")      # git-ignored; clean_up.py removes it


def write_parquet_batch(events: list[dict], root: Path, arrived: datetime, batch: int) -> Path:
    """One Firehose delivery: a batch of buffered records as one Parquet file under the arrival day."""
    folder = root / "updates" / f"dt={arrived:%Y-%m-%d}"
    folder.mkdir(parents=True, exist_ok=True)
    table = pa.table({
        "event_id": pa.array([e["event_id"] for e in events], pa.string()),
        "entity_id": pa.array([e["entity_id"] for e in events], pa.string()),
        "event_time": pa.array([parse(e["event_time"]).replace(tzinfo=None) for e in events], pa.timestamp("ms")),
        "value": pa.array([int(e["value"]) for e in events], pa.int32()),
    })
    path = folder / f"{arrived:%Y%m%d%H%M%S}-{batch:04d}.parquet"
    pq.write_table(table, path, compression="snappy")      # Firehose's Parquet conversion also uses Snappy
    return path


def write_export(items: list[dict], root: Path, export_id: str) -> Path:
    """A full DynamoDB export: lines of {"Item": {<attribute>: {<type>: value}}}, gzip-compressed."""
    folder = root / "exports" / "AWSDynamoDB" / export_id / "data"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "part-0000.json.gz"
    with gzip.open(path, "wt") as f:
        for it in items:
            f.write(json.dumps({"Item": it}) + "\n")
    return folder


def connect(root: Path) -> duckdb.DuckDBPyConnection:
    """DuckDB with the same table names Athena sees: `updates` and `updates_export`."""
    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    if list(root.glob("updates/dt=*/*.parquet")):
        con.execute(f"CREATE VIEW updates AS SELECT * FROM read_parquet('{root}/updates/dt=*/*.parquet', "
                    "hive_partitioning = true, hive_types = {'dt': VARCHAR})")
    exports = sorted(root.glob("exports/AWSDynamoDB/*/data"), key=lambda p: p.stat().st_mtime)
    if exports:                                   # the newest export, as rep-operational-first points Athena at it
        con.execute(f"CREATE VIEW updates_export AS SELECT * FROM read_json('{exports[-1]}/*.json.gz', "
                    "format = 'newline_delimited', union_by_name = true)")
        con.execute(f"CREATE VIEW events AS {EVENTS_VIEW_SQL}")
    return con


def query(root: Path, sql: str) -> list[dict]:
    con = connect(root)
    cur = con.execute(sql)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def now() -> datetime:
    return datetime.now(timezone.utc)

"""The offline stand-in for the Glue job (and for Athena's MERGE): the same steps as etl_job.py's main(), with DuckDB
for Spark and the Iceberg table. No PySpark. The scenarios answer StartJobRun and StartQueryExecution with it.

The table lives in a DuckDB file, $ETL_LOCAL_DB (default .local/s3-event-etl/lake.duckdb).
"""
from __future__ import annotations

import os
from pathlib import Path

import etl_job

TYPES = {"string": "VARCHAR", "bigint": "BIGINT", "double": "DOUBLE", "timestamp": "TIMESTAMPTZ", "date": "DATE"}


def db_path() -> Path:
    return Path(os.environ.get("ETL_LOCAL_DB") or Path(".local") / "s3-event-etl" / "lake.duckdb")


def connect():
    import duckdb
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    con.execute(f"CREATE TABLE IF NOT EXISTS {etl_job.TABLE} ("
                + ", ".join(f"{c} {TYPES[t]}" for c, t in etl_job.COLUMNS) + ")")
    return con


def load(s3, bucket: str, days: list[str], fail_after_merge: bool = False, manifests: bool = True) -> dict:
    """Load what is waiting; returns {"objects", "rows_in", "inserted"}. Raises RuntimeError("step=...") on a fault."""
    todo = etl_job.waiting(s3, bucket, days)
    keys = [k for ks in todo.values() for k in ks]
    step = "load"
    try:
        rows = []
        for k in keys:
            rows += etl_job.rows_of(s3.get_object(Bucket=bucket, Key=k)["Body"].read().decode(), k)
        rows = etl_job.first_per_event(rows)
        con = connect()
        have = {r[0] for r in con.execute(f"SELECT event_id FROM {etl_job.TABLE}").fetchall()}
        new = [r for r in rows if r["event_id"] not in have]          # MERGE ... WHEN NOT MATCHED THEN INSERT
        cols = [c for c, _ in etl_job.COLUMNS]
        for r in new:
            r = {**r, "event_day": r["event_time"][:10]}
            con.execute(f"INSERT INTO {etl_job.TABLE} VALUES ({', '.join('?' for _ in cols)})",
                        [r.get(c) for c in cols])
        con.close()
        step = "manifest"
        if fail_after_merge:
            raise RuntimeError("FAIL_AFTER_MERGE: stopped after the commit, before the manifest")
        for day, ks in (todo.items() if manifests else []):
            etl_job.append_manifest(s3, bucket, day, ks)
    except Exception as err:
        raise RuntimeError(f"step={step}: {err}") from err
    return {"objects": len(keys), "rows_in": len(rows), "inserted": len(new)}


def count() -> int:
    con = connect()
    n = con.execute(f"SELECT count(*) FROM {etl_job.TABLE}").fetchone()[0]
    distinct = con.execute(f"SELECT count(DISTINCT event_id) FROM {etl_job.TABLE}").fetchone()[0]
    con.close()
    assert n == distinct, (n, distinct)
    return n

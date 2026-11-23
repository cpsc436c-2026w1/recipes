"""results.duckdb: every run of the experiment, kept or excluded, and what the summary reads.

Tables: athena_runs (R60, R70), duckdb_runs (R100), result_rows (each run's rows, for R110), growth (the probe of each
size and the note), prices (prices.toml). summary.sql reads them; nothing is computed outside SQL for the summary.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import duckdb

from layout import billed_gb_s, billed_mb, exclusion, spend_billed_mb

HERE = Path(__file__).resolve().parent
SCHEMA = """
CREATE TABLE IF NOT EXISTS athena_runs (
  run_id VARCHAR, engine VARCHAR, layout VARCHAR, copies INTEGER, query VARCHAR, run_no INTEGER,
  query_execution_id VARCHAR, state VARCHAR, state_reason VARCHAR, reused BOOLEAN,
  data_scanned_bytes BIGINT, billed_mb BIGINT, spend_mb BIGINT,
  engine_ms DOUBLE, queue_ms DOUBLE, planning_ms DOUBLE, to_last_row_ms DOUBLE,
  excluded BOOLEAN, reason VARCHAR);
CREATE TABLE IF NOT EXISTS duckdb_runs (
  run_id VARCHAR, engine VARCHAR, layout VARCHAR, copies INTEGER, query VARCHAR, run_no INTEGER,
  request_id VARCHAR, duration_ms DOUBLE, billed_duration_ms DOUBLE, init_duration_ms DOUBLE, memory_mb INTEGER,
  billed_gb_s DOUBLE, cold BOOLEAN, s3_gets BIGINT, result_rows INTEGER, to_last_row_ms DOUBLE,
  excluded BOOLEAN, reason VARCHAR, s3_lists BIGINT);
CREATE TABLE IF NOT EXISTS result_rows (
  run_id VARCHAR, engine VARCHAR, layout VARCHAR, copies INTEGER, query VARCHAR, run_no INTEGER,
  pickup_zone_id INTEGER, trips BIGINT);
CREATE TABLE IF NOT EXISTS growth (run_id VARCHAR, copies INTEGER, probe_mb DOUBLE, note VARCHAR, sizes VARCHAR,
  bound_usd DOUBLE);
CREATE TABLE IF NOT EXISTS prices (name VARCHAR, usd DOUBLE, price_date VARCHAR);
"""


def connect(path: Path) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(str(path))
    con.execute(SCHEMA)
    con.execute("ALTER TABLE duckdb_runs ADD COLUMN IF NOT EXISTS s3_lists BIGINT")  # a results.duckdb made before it
    return con


def load_prices(con, path: Path = HERE / "prices.toml") -> dict:
    p = tomllib.loads(path.read_text())
    con.execute("DELETE FROM prices")
    for k, v in p.items():
        if isinstance(v, (int, float)):
            con.execute("INSERT INTO prices VALUES (?, ?, ?)", [k, float(v), p["date"]])
    return p


def store_athena(con, run_id: str, run, *, query_execution_id: str, state: str, reason: str = "",
                 reused: bool = False, data_scanned_bytes: int = 0, engine_ms=None, queue_ms=None,
                 planning_ms=None, to_last_row_ms=None) -> dict:
    why = exclusion(state, reused, reason)
    row = dict(run_id=run_id, engine="athena", layout=run.layout, copies=run.copies, query=run.query,
               run_no=run.run_no, query_execution_id=query_execution_id, state=state, state_reason=reason,
               reused=reused, data_scanned_bytes=int(data_scanned_bytes),
               billed_mb=billed_mb(data_scanned_bytes, state, reused),
               spend_mb=spend_billed_mb(state, data_scanned_bytes, reused, reason), engine_ms=engine_ms,
               queue_ms=queue_ms, planning_ms=planning_ms, to_last_row_ms=to_last_row_ms, excluded=bool(why),
               reason=why)
    con.execute(f"INSERT INTO athena_runs ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", list(row.values()))
    return row


def store_duckdb(con, run_id: str, run, *, request_id: str, duration_ms: float, billed_duration_ms: float,
                 init_duration_ms, memory_mb: int, s3_gets, result_rows: int, to_last_row_ms: float,
                 error: str = "", s3_lists=None) -> dict:
    row = dict(run_id=run_id, engine="duckdb", layout=run.layout, copies=run.copies, query=run.query,
               run_no=run.run_no, request_id=request_id, duration_ms=duration_ms,
               billed_duration_ms=billed_duration_ms, init_duration_ms=init_duration_ms, memory_mb=memory_mb,
               billed_gb_s=billed_gb_s(billed_duration_ms, memory_mb), cold=init_duration_ms is not None,
               s3_gets=s3_gets, result_rows=result_rows, to_last_row_ms=to_last_row_ms, excluded=bool(error),
               reason=error[:200], s3_lists=s3_lists)
    con.execute(f"INSERT INTO duckdb_runs ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", list(row.values()))
    return row


def store_rows(con, run_id: str, run, engine: str, rows) -> None:
    for zone, trips in rows:
        con.execute("INSERT INTO result_rows VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [run_id, engine, run.layout, run.copies, run.query, run.run_no, int(zone), int(trips)])


def summary(con, sql_path: Path = HERE / "summary.sql") -> tuple[list[str], list[tuple]]:
    """The cells of summary.sql: its last statement's columns and rows."""
    cur = con.execute(sql_path.read_text())
    return [d[0] for d in cur.description], cur.fetchall()

"""DuckDB over the day's trips, with the outside world switched off (README, Decisions: one read-only SQL tool).

Adapted from dashboard-chat's main/engine.py (not a shared copy: this one has one table, a home directory for
Lambda and a time limit the caller can shorten). Two layers keep the model's SQL harmless:
1. sqlcheck.check_sql lets one SELECT over `trips` through, nothing else;
2. this engine holds the table in memory, then turns off file and network access and locks the settings, and runs
   each query as one statement in a read-only transaction.

In Lambda only /tmp can be written, and DuckDB looks for a home directory when it starts an extension or a
secret manager: `home_directory` is set to /tmp before the configuration is locked.
"""
from __future__ import annotations

import threading
from pathlib import Path

import duckdb

MAX_ROWS = 50          # rows returned to the model per query: enough for a breakdown by zone or hour, small in tokens
QUERY_TIMEOUT_S = 5.0  # seconds one query may run: the recipe's own queries take milliseconds over 19,074 rows
TABLE = "trips"


class Engine:
    def __init__(self, trips_path: Path | str):
        self.con = duckdb.connect(":memory:")
        self.con.execute(f"CREATE TABLE {TABLE} AS SELECT * FROM read_parquet(?)", [str(trips_path)])
        self.con.execute("SET home_directory = '/tmp'")
        self.con.execute("SET enable_external_access = false")
        self.con.execute("SET lock_configuration = true")
        self.statements_run = 0          # statements DuckDB executed for the model (the tests read it)

    def count(self) -> int:
        return self.con.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0]

    def run(self, sql: str, timeout_s: float = QUERY_TIMEOUT_S) -> dict:
        """One statement in a read-only transaction, interrupted after timeout_s (the caller passes the time left
        to the question's deadline when that is shorter). Raises duckdb.Error with the reason."""
        statements = self.con.extract_statements(sql)
        if len(statements) != 1:
            raise duckdb.InvalidInputException(f"the engine runs one statement; this SQL holds {len(statements)}")
        cur = self.con.cursor()
        cur.execute("BEGIN TRANSACTION READ ONLY")
        timer = threading.Timer(max(timeout_s, 0.001), cur.interrupt)
        timer.start()
        try:
            self.statements_run += 1
            cur.execute(statements[0])
            columns = [d[0] for d in cur.description or []]
            rows = cur.fetchmany(MAX_ROWS + 1)
        except duckdb.InterruptException:
            raise duckdb.InterruptException(f"query stopped after {timeout_s:.1f} s, the time it had left") from None
        finally:
            timer.cancel()
            try:
                cur.execute("ROLLBACK")
            except duckdb.Error:
                pass
        return {"columns": columns, "rows": [[_plain(v) for v in r] for r in rows[:MAX_ROWS]],
                "truncated": len(rows) > MAX_ROWS}


def _plain(v):
    """JSON-friendly values: decimals become numbers, dates and times strings."""
    if v is None or isinstance(v, (int, float, str, bool)):
        return v
    try:
        return float(v)
    except (TypeError, ValueError):
        return str(v)

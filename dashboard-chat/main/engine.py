"""DuckDB with the team's tables loaded and the outside world switched off.

Two layers keep the model's SQL harmless (README, "Decisions": read-only SQL):
1. sqlcheck.check_sql lets one SELECT over the allowed tables through, nothing else;
2. this engine holds copies of the tables in memory, then turns off file and network access and locks the settings,
   and runs each query as one statement in a read-only transaction. So even a query that slipped past the check
   cannot read a file, write one, change a setting or change the tables.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from pathlib import Path

import duckdb

MAX_ROWS = 50  # rows returned to the model and the dashboard per query (ours: enough for a chart, small in tokens)
MAX_CELL_CHARS = 200  # characters of one text value returned (ours: a name or a title fits, a document does not)
QUERY_TIMEOUT_S = 5.0  # seconds one query may run (ours: the recipe's own queries take a few milliseconds)


class Engine:
    def __init__(self, paths: dict[str, Path], only_rows: dict[str, tuple[str, str]] | None = None,
                 query_timeout_s: float = QUERY_TIMEOUT_S):
        """only_rows: table -> (column, value). Of such a table, only the rows with that value are loaded."""
        self.paths = paths
        self.query_timeout_s = query_timeout_s
        self.con = duckdb.connect(":memory:")
        for table, path in paths.items():
            if only_rows and table in only_rows:
                column, value = only_rows[table]
                self.con.execute(f'CREATE TABLE {table} AS SELECT * FROM read_parquet(?) WHERE "{column}" = ?',
                                 [str(path), value])
            else:
                self.con.execute(f"CREATE TABLE {table} AS SELECT * FROM read_parquet(?)", [str(path)])
        self.con.execute("SET enable_external_access = false")
        self.con.execute("SET lock_configuration = true")
        self.tables = tuple(paths)

    def for_rows_of(self, own_rows: dict[str, str], value: str) -> "Engine":
        """A second engine for one user: of each table in own_rows (table -> column) it holds only the rows whose
        column equals value. Rows that were never loaded cannot be returned, whatever the SQL says
        (ext-guardrails-redteam). It reads the Parquet files again: fine for a small table; for a large one keep
        one engine per signed-in user, or filter in the store the rows come from."""
        return Engine(self.paths, only_rows={table: (column, value) for table, column in own_rows.items()},
                      query_timeout_s=self.query_timeout_s)

    def schema(self) -> dict[str, list[tuple[str, str]]]:
        """Column names and types per table, for the system prompt and the SQL check."""
        return {t: [(r[0], r[1]) for r in self.con.execute(f"DESCRIBE {t}").fetchall()] for t in self.tables}

    @contextmanager
    def cursor(self, sql: str):
        """Run one statement and give its cursor to the caller, with every limit of this engine in force.

        Decision (second layer): one statement, in a read-only transaction. DuckDB itself then refuses a write
        that slipped past the SQL check, and the tables stay as they were loaded. One statement only, because a
        second statement could end the transaction first.
        Decision (bounded work): a query the LLM wrote may join every row with every row. A timer interrupts it
        after query_timeout_s, and the caller gets the time limit as the error.
        run() below and the Shiny app (ext-shiny-app/source.py) both read their rows through here.
        """
        statements = self.con.extract_statements(sql)
        if len(statements) != 1:
            raise duckdb.InvalidInputException(f"the engine runs one statement; this SQL holds {len(statements)}")
        cur = self.con.cursor()
        cur.execute("BEGIN TRANSACTION READ ONLY")
        timer = threading.Timer(self.query_timeout_s, cur.interrupt)
        timer.start()
        try:
            cur.execute(statements[0])
            yield cur
        except duckdb.InterruptException:
            raise duckdb.InterruptException(
                f"query stopped after {self.query_timeout_s:g} s, the time limit of one query; "
                f"write a query that reads fewer rows") from None
        finally:
            timer.cancel()
            try:
                cur.execute("ROLLBACK")
            except duckdb.Error:
                pass  # the statement itself ended the transaction

    def run(self, sql: str) -> dict:
        with self.cursor(sql) as cur:
            columns = [d[0] for d in cur.description or []]
            rows = cur.fetchmany(MAX_ROWS + 1)
        # Decision (bounded work): the result is cut to MAX_ROWS rows and MAX_CELL_CHARS characters per text value.
        # "truncated" says what was cut ("rows", "text", "rows and text"; None when nothing was), so the LLM and
        # the trace know that the result is partial.
        cut = ["rows"] if len(rows) > MAX_ROWS else []
        rows = [[_plain(v) for v in r] for r in rows[:MAX_ROWS]]
        if any(isinstance(v, str) and len(v) > MAX_CELL_CHARS for r in rows for v in r):
            cut.append("text")
        rows = [[v[:MAX_CELL_CHARS] if isinstance(v, str) else v for v in r] for r in rows]
        return {"columns": columns, "rows": rows, "truncated": " and ".join(cut) or None}


def _plain(v):
    """JSON-friendly values: decimals and dates become numbers and strings."""
    if v is None or isinstance(v, (int, float, str, bool)):
        return v
    try:
        return float(v)
    except (TypeError, ValueError):
        return str(v)

"""Read-only access to the team's tables: a statement check, a locked DuckDB, a row cap.

Three layers, so a gap in one does not reach the data (README, "Decisions": read-only query):

1. `check_select` asks DuckDB's own parser what the text is. Only one statement of type SELECT goes on.
   The parser is the one that will run the query, so odd spelling or comments cannot hide a second statement.
2. The connection holds in-memory copies of the Parquet files. After loading, file and network access are switched
   off and the settings are locked, so even a SELECT cannot read another file (`read_csv('/etc/passwd')`) or turn
   access back on. A query cannot change the Parquet files: the connection does not have them open.
3. Every query runs in a read-only transaction that is rolled back. The parser calls a statement SELECT when a
   CTE in it writes (`WITH d AS (DELETE FROM orders RETURNING *) SELECT * FROM d`), so layer 1 lets it through;
   the read-only transaction refuses the write, and the in-memory copies stay as loaded.
"""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import duckdb


class QueryRefused(ValueError):
    """The text is not one SELECT statement. The message says why, in words the model can act on; `kind` is one
    word for the log (empty, does_not_parse, several_statements, or the statement's type: DELETE, DROP, ...)."""

    def __init__(self, message: str, kind: str):
        super().__init__(message)
        self.kind = kind


class QueryTimedOut(RuntimeError):
    """The query ran longer than the engine's time limit and was stopped."""


QUERY_TIMEOUT_S = 5.0     # ours: the sample tables answer in milliseconds; set it from your slowest real query
MAX_CELL_CHARS = 200      # ours: the most characters of one text value that reach the model
MAX_RESULT_CHARS = 20_000  # ours: the most characters of all rows of one result, as JSON: about 5,000 tokens
MAX_COLUMNS = 100         # ours: wider than a table a model should read in one result; with it, one row has a bound
MEMORY_LIMIT = "1GB"      # ours: far above what the sample tables need; one query cannot take the machine's memory


def check_select(sql: str) -> str:
    text = sql.strip().rstrip(";").strip()
    if not text:
        raise QueryRefused("empty query; send one SELECT statement", "empty")
    try:
        statements = duckdb.extract_statements(text)
    except duckdb.Error as e:
        raise QueryRefused(f"the query does not parse: {e}", "does_not_parse") from None
    if len(statements) != 1:
        raise QueryRefused(f"send one statement, not {len(statements)}", "several_statements")
    kind = statements[0].type
    if kind != duckdb.StatementType.SELECT:
        raise QueryRefused(f"only SELECT is allowed; this is {kind.name}", kind.name)
    return text


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[list]
    truncated: bool           # more rows matched than max_rows
    text_cut: bool = False    # one or more text values were longer than MAX_CELL_CHARS and were cut
    columns_cut: bool = False  # the query returned more than MAX_COLUMNS columns; the first MAX_COLUMNS are here
    note: str = ""            # what to do when even the first row did not fit in MAX_RESULT_CHARS


class ReadOnlyEngine:
    def __init__(self, tables: dict[str, Path], max_rows: int = 50, query_timeout_s: float = QUERY_TIMEOUT_S):
        self.max_rows = max_rows
        self.query_timeout_s = query_timeout_s
        self.con = duckdb.connect(":memory:")
        for name, path in tables.items():
            # name comes from data.TABLES (our code), never from a request
            self.con.execute(f"CREATE TABLE {name} AS SELECT * FROM read_parquet('{path}')")
        self.con.execute("SET enable_external_access = false")
        # Before the lock: after it, no query can raise the limit, and this code cannot set it either.
        self.con.execute(f"SET memory_limit = '{MEMORY_LIMIT}'")
        self.con.execute("SET lock_configuration = true")
        # A DuckDB connection is not safe to share between threads; the server may run tool calls on several.
        self._lock = threading.Lock()

    def schema(self) -> list[dict]:
        with self._lock:
            cols = self.con.execute(
                "SELECT table_name, column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = 'main' ORDER BY table_name, ordinal_position").fetchall()
            tables: dict[str, dict] = {}
            for table, column, dtype in cols:
                tables.setdefault(table, {"table": table, "columns": []})["columns"].append(
                    {"name": column, "type": dtype})
            for t in tables.values():
                t["rows"] = self.con.execute(f'SELECT count(*) FROM "{t["table"]}"').fetchone()[0]
        return list(tables.values())

    def query(self, sql: str) -> QueryResult:
        text = check_select(sql)
        with self._lock, self._read_only():
            cur = self.con.execute(text)
            columns = [d[0] for d in cur.description]
            # fetch one row past the cap: that tells "exactly max_rows" from "more than max_rows"
            rows = cur.fetchmany(self.max_rows + 1)
        truncated = len(rows) > self.max_rows
        # A row has no bound without a limit on its columns: 500 columns of 200 characters are one row of 100,000.
        columns_cut = len(columns) > MAX_COLUMNS
        columns = columns[:MAX_COLUMNS]
        kept = [[_plain(v) for v in r[:MAX_COLUMNS]] for r in rows[: self.max_rows]]
        # The row cap bounds how many rows reach the model, not how long each value is: one long text column would
        # still fill the model's context. Cut each text value, and say so, so a cut result is not read as whole.
        text_cut = any(isinstance(v, str) and len(v) > MAX_CELL_CHARS for r in kept for v in r)
        kept = [[v[:MAX_CELL_CHARS] if isinstance(v, str) else v for v in r] for r in kept]
        # 50 rows of 200 characters are small; 50 rows of 40 such columns are not. The whole result has a limit
        # too: rows are dropped from the end until the rest fits, and `truncated` says that rows are missing.
        sizes = [len(json.dumps(r)) for r in kept]
        had_rows = bool(kept)
        while kept and sum(sizes) > MAX_RESULT_CHARS:
            kept.pop()
            sizes.pop()
            truncated = True
        # No row left: the first row alone was over the limit. An empty result that only says `truncated` reads
        # as "no rows matched", so the result says what happened and what to do.
        note = (f"the first row alone is over {MAX_RESULT_CHARS:,} characters as JSON, so no row is returned: "
                "select fewer columns" if had_rows and not kept else "")
        return QueryResult(columns, kept, truncated, text_cut, columns_cut, note)

    def fetch_one(self, sql: str, params: list) -> tuple | None:
        """For app code (not a tool): one row of a fixed query with parameters, for example an order by its id."""
        text = check_select(sql)
        with self._lock, self._read_only():
            return self.con.execute(text, params).fetchone()

    @contextmanager
    def _read_only(self):
        """A read-only transaction around one query, always rolled back: a write inside a SELECT is refused.

        A timer interrupts the query after query_timeout_s. A SELECT the model wrote may join every row with every
        row; without the limit it would hold the engine, and every other tool call would wait behind it."""
        self.con.execute("BEGIN TRANSACTION READ ONLY")
        timer = threading.Timer(self.query_timeout_s, self.con.interrupt)
        timer.start()
        try:
            yield
        except duckdb.InterruptException:
            raise QueryTimedOut(f"query stopped after {self.query_timeout_s:g} s, the time limit of one query; "
                                "write a query that reads fewer rows") from None
        finally:
            timer.cancel()
            try:
                self.con.execute("ROLLBACK")
            except duckdb.Error:
                pass          # the interrupted statement already ended the transaction

    def close(self) -> None:
        self.con.close()


def _plain(value):
    """JSON-friendly cell: dates and decimals become text or float, so the tool result is plain JSON."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    try:
        return float(value)
    except (TypeError, ValueError):
        return str(value)

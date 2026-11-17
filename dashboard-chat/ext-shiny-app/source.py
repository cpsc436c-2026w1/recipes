"""The app's data source: querychat asks, the recipe's SQL check and read-only engine answer.

querychat (https://posit-dev.github.io/querychat/) runs the SQL the LLM writes through a "data source". Its own
DuckDB source checks the first keyword of a query. This one sends every query through the same two layers as the
command-line recipe (README, "Decisions": checking the SQL, second layer):
1. main/sqlcheck.py: one SELECT over the team's tables, no file or environment functions;
2. main/engine.py: one statement, in a read-only transaction, for at most 5 seconds, with file access off.
The result comes back as a pandas data frame, which is what querychat and Shiny's table take.

The table on the page gets every row of a filter. The LLM does not: what it reads is the result of the tool
run_sql below, cut to the recipe's 50 rows and 200 characters per text value, with "truncated" saying so.
"""

from __future__ import annotations

import html

import chatlas
from chatlas.types import ContentToolResult
from shinychat.types import ToolResultDisplay
from querychat._datasource import DataFrameSource, MissingColumnsError

from main.engine import Engine
from main.sqlcheck import SqlRejected, check_sql


class RecipeSource(DataFrameSource):
    """One table of the recipe's engine, for querychat. A subclass of querychat's own DuckDB source: the schema
    and the column statistics for the system prompt come from there, the two methods that run SQL are ours."""

    def __init__(self, engine: Engine, table_name: str):
        # No call to DataFrameSource.__init__: it copies a data frame into a DuckDB connection of its own. The
        # tables are already in the engine's connection, locked, and that is the connection this source reads.
        self.engine = engine
        self.table_name = table_name
        self._conn = engine.con
        self._df_lib = "pandas"
        self._colnames = [column for column, _type in engine.schema()[table_name]]

    def _frame(self, query: str, limit: int | None = None):
        sql = query.strip().rstrip(";").strip()
        check_sql(sql, self.engine.tables)  # raises SqlRejected with a message the LLM can act on
        if limit is not None:
            sql = f"SELECT * FROM ({sql}) AS q LIMIT {limit}"
        with self.engine.cursor(sql) as cur:
            return cur.df()

    def execute_query(self, query: str):
        return self._frame(query)

    def test_query(self, query: str, *, require_all_columns: bool = False):
        frame = self._frame(query, limit=1)
        missing = [c for c in self._colnames if c not in frame.columns]
        if require_all_columns and missing:
            raise MissingColumnsError(
                f"Query result missing required columns: {', '.join(missing)}. A filter of the dashboard returns "
                f"every column of {self.table_name}: {', '.join(self._colnames)}")
        return frame

    def get_data(self):
        return self._frame(f'SELECT * FROM "{self.table_name}"')

    def cleanup(self) -> None:
        pass  # the engine's connection belongs to the app, not to one chat session


def run_sql_tool(engine: Engine) -> chatlas.Tool:
    """The page's query tool: the recipe's run_sql (main/prompts.py, main/chat.py), for a chatlas client.

    Decision (bounded work): querychat's own query tool gives the LLM every row of a result, and the LLM gets them
    again with each later call of the conversation. This tool takes its place: the SQL check, then Engine.run,
    which returns at most MAX_ROWS rows and MAX_CELL_CHARS characters per text value and says what it cut.
    """

    def run_sql(sql: str) -> ContentToolResult:
        """Run one read-only DuckDB SELECT over the tables and return the rows (at most 50). Use it to answer a
        question with a number or a small table. Take every number in your reply from the rows."""
        shown = f"```sql\n{sql}\n```"
        try:
            check_sql(sql.strip().rstrip(";").strip(), engine.tables)
            result = engine.run(sql.strip().rstrip(";").strip())
        except SqlRejected as e:
            return ContentToolResult(value=f"SQL check: {e}", error=Exception(f"SQL check: {e}"))
        except Exception as e:  # DuckDB errors go back to the LLM so it can fix the query (first line only)
            message = f"DuckDB: {str(e).splitlines()[0][:200]}"
            return ContentToolResult(value=message, error=Exception(message))
        head = "".join(f"<th>{html.escape(str(c))}</th>" for c in result["columns"])
        body = "".join("<tr>" + "".join(f"<td>{html.escape(str(v))}</td>" for v in row) + "</tr>"
                       for row in result["rows"][:5])
        note = f"\n\nThe result was cut ({result['truncated']})." if result["truncated"] else ""
        table = f'<table class="table table-sm"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'
        return ContentToolResult(
            value=result,  # {"columns": [...], "rows": [...], "truncated": None | "rows" | "text" | "rows and text"}
            extra={"display": ToolResultDisplay(markdown=f"{shown}\n\n{table}{note}", show_request=False,
                                                open=True)})

    return chatlas.Tool.from_func(run_sql, name="run_sql", annotations={"title": "Query the tables"})

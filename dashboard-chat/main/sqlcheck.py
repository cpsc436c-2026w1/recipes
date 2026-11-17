"""App code checks the model's SQL before DuckDB runs it (README, "Decisions": read-only SQL, checked in app code).

The check is written as rules over the parsed query (sqlglot), not as a search for words like DROP: a word list
misses a write hidden in a comment trick and refuses a column called `updated`. A rejection message goes back to the
model as the tool result, so the model can fix its SQL on the next turn.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

import sqlglot
from sqlglot import exp

# Table functions that read files or the environment. The engine blocks them too; the check says why earlier.
BLOCKED_FUNCTIONS = {
    "read_csv", "read_csv_auto", "read_parquet", "read_json", "read_json_auto", "read_text", "read_blob",
    "glob", "getenv", "parquet_scan", "csv_scan", "sniff_csv", "query", "query_table",
}

SqlRule = Callable[[exp.Expression], None]  # raises SqlRejected; used by ext-guardrails-redteam


class SqlRejected(ValueError):
    pass


def check_sql(sql: str, allowed_tables: Iterable[str], extra_rules: Iterable[SqlRule] = ()) -> exp.Expression:
    """Return the parsed query, or raise SqlRejected with a message the model can act on."""
    allowed = {t.lower() for t in allowed_tables}
    text = sql.strip().rstrip(";").strip()
    try:
        statements = [s for s in sqlglot.parse(text, dialect="duckdb") if s is not None]
    except sqlglot.errors.SqlglotError as e:
        raise SqlRejected(f"the SQL does not parse: {str(e).splitlines()[0][:200]}") from None
    if len(statements) != 1:
        raise SqlRejected(f"send exactly one statement; this SQL holds {len(statements)}")
    query = statements[0]
    if not isinstance(query, exp.Query):
        raise SqlRejected(f"only one SELECT is allowed; this is a {query.key.upper()} statement")
    for node in query.walk():
        if isinstance(node, (exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Drop, exp.Command, exp.Copy)):
            raise SqlRejected(f"only reading is allowed; found {node.key.upper()}")
    for func in query.find_all(exp.Func):
        name = (func.name if isinstance(func, exp.Anonymous) else func.sql_name()).lower()
        if name in BLOCKED_FUNCTIONS or name.startswith("read_"):
            raise SqlRejected(f"the function {name} is not allowed; query the tables {sorted(allowed)}")
    ctes = {c.alias_or_name.lower() for c in query.find_all(exp.CTE)}
    for table in query.find_all(exp.Table):
        if not isinstance(table.this, exp.Identifier):
            raise SqlRejected("FROM must name a table, not a file or a function")
        if table.args.get("db") or table.args.get("catalog"):
            raise SqlRejected("use the plain table names, with no schema or database in front")
        name = table.name.lower()
        if name not in allowed and name not in ctes:
            raise SqlRejected(f"unknown table {name}; the tables are {sorted(allowed)}")
    for rule in extra_rules:
        rule(query)
    return query

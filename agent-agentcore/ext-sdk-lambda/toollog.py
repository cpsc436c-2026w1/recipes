"""The one tool, run_sql, and the tool log: every call with its SQL, whether it ran, and its rows.

The answer's SQL and rows are taken from this log, never from the model's output (README, Decisions): the model
writes its output, so it can name a query it never ran or a number no query returned. Both SDKs call the same
SqlTool, so the log is the same whichever runs the loop.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import duckdb
from sqlglot import exp

from engine import QUERY_TIMEOUT_S, TABLE, Engine
from sqlcheck import SqlRejected, check_sql

NO_TABLE = (f"the query reads no table {TABLE}; every number must come from {TABLE}: compute a difference or a sum "
            f"in this one SELECT, as in SELECT (SELECT COUNT(*) FROM {TABLE} WHERE ...) - "
            f"(SELECT COUNT(*) FROM {TABLE} WHERE ...) AS difference")


def reads_table(query: exp.Expression, table: str = TABLE) -> None:
    """R120: the SELECT reads the table itself, anywhere in it (a subquery counts). A SELECT of constants, or of a
    CTE that holds only constants, would put any number the model likes into the tool log, and the value check
    (verdict.py) would then pass it. A CTE named like the table would hide it, so that is refused too. An output
    column made of numeric constants only, in any SELECT of the query (`SELECT 7661 - 7068 AS d FROM trips`,
    `SELECT 161 AS zone, COUNT(*) ...`), is refused as well: its number would sit in the rows beside the data. What
    still passes is a constant inside an expression that also reads the data (`COUNT(*) + 5`): the check is that
    each output column is computed from the data, not that it equals it. A VALUES list is refused as a table of
    constants; the SELECT inside an EXISTS is left alone, since its output is never a result row."""
    ctes = {c.alias_or_name.lower() for c in query.find_all(exp.CTE)}
    if table in ctes:
        raise SqlRejected(f"do not name a CTE {table}; give it another name")
    if table not in {t.name.lower() for t in query.find_all(exp.Table)}:
        raise SqlRejected(NO_TABLE)
    if query.find(exp.Values):
        raise SqlRejected(f"a VALUES list is a table of constants; {CONSTANT_COLUMN}")
    for select in query.find_all(exp.Select):
        if select.find_ancestor(exp.Exists):
            continue    # EXISTS (SELECT 1 FROM trips ...) tests for rows; its output column is never a result
        for column in select.expressions:
            if constant_number(column):
                raise SqlRejected(f"the output column {column.sql()} is a constant; {CONSTANT_COLUMN}")


DATA = (exp.Column, exp.Star, exp.AggFunc, exp.Subquery, exp.Window)
CONSTANT_COLUMN = (f"every output column must be computed from {TABLE}: select the column itself (pickup_zone_id), "
                   f"or compute a difference or a sum from subqueries over {TABLE}")


def constant_number(column: exp.Expression) -> bool:
    """True when an output column holds a numeric constant and reads nothing: no column, *, aggregate, subquery or
    window function anywhere in it."""
    if column.find(*DATA):
        return False
    return any(lit.is_number for lit in column.find_all(exp.Literal))


class DeadlineReached(Exception):
    """The question's deadline passed before a call could start: the question ends `stopped`."""


@dataclass
class Deadline:
    """One absolute deadline for the question: its start plus `seconds`, on the monotonic clock."""
    seconds: float
    clock: callable = time.monotonic
    start: float = field(default=0.0)

    def __post_init__(self):
        self.start = self.clock()

    @property
    def at(self) -> float:
        return self.start + self.seconds

    def left(self) -> float:
        return self.at - self.clock()

    def passed(self) -> bool:
        return self.left() <= 0

    def elapsed(self) -> float:
        return self.clock() - self.start


@dataclass
class SqlTool:
    engine: Engine
    deadline: Deadline
    log: list[dict] = field(default_factory=list)

    def __call__(self, sql: str) -> dict:
        """The tool's result for the model: columns and rows, or {"error": why}. Every call goes to the log."""
        if self.deadline.passed():
            raise DeadlineReached(f"deadline of {self.deadline.seconds:g} s")
        entry = {"sql": sql, "status": "rejected", "columns": [], "rows": []}
        self.log.append(entry)
        try:
            reads_table(check_sql(sql, [TABLE]))
        except SqlRejected as err:
            entry["error"] = f"rejected: {err}"
            return {"error": entry["error"]}
        try:
            result = self.engine.run(sql, timeout_s=min(QUERY_TIMEOUT_S, self.deadline.left()))
        except duckdb.Error as err:
            entry["status"], entry["error"] = "failed", f"{type(err).__name__}: {str(err).splitlines()[0][:300]}"
            return {"error": entry["error"]}
        entry.update(status="ok", columns=result["columns"], rows=result["rows"], truncated=result["truncated"])
        return result

    @property
    def succeeded(self) -> list[dict]:
        return [e for e in self.log if e["status"] == "ok"]

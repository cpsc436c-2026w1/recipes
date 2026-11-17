"""The data the LLM step answers questions about, and the three question splits.

Decision (README, Decisions: "Three splits"): the optimizer gets train and validation through `load_dev()`.
Only the final report gets the test questions, through `load_test()`. tests/test_splits.py checks that
main/optimize.py never calls `load_test()` and that no test question is in questions.yml.
"""

from __future__ import annotations

import threading
from pathlib import Path

import duckdb
import yaml

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"

# The schema the model sees in every call. It is an input of the signature, not part of the instructions,
# so GEPA cannot rewrite it (README, Decisions: "What GEPA may change").
SCHEMA = """Table orders: one row per order at a small cafe on one day.
  order_id   INTEGER
  placed_at  TIMESTAMP   when the customer ordered
  ready_at   TIMESTAMP   when the order was ready
  item       VARCHAR     latte, drip coffee, tea, muffin, breakfast sandwich, soup
  quantity   INTEGER     how many of the item
  price_cad  DOUBLE      price of one item, in dollars
  payment    VARCHAR     card or cash
  tip_cad    DOUBLE      tip on the order, in dollars"""

_con = duckdb.connect()
_con.execute(f"CREATE TABLE orders AS SELECT * FROM read_csv('{DATA / 'orders.csv'}', header = true)")
_lock = threading.Lock()  # one DuckDB connection, shared by GEPA's worker threads


def _load(name: str) -> list[dict]:
    return yaml.safe_load((DATA / name).read_text())


def load_dev() -> dict[str, list[dict]]:
    """Train and validation questions, each with its reference value. The optimizer uses only these."""
    qs = _load("questions.yml")
    for q in qs:
        q["reference"] = reference_value(q["sql"])
    return {"train": [q for q in qs if q["split"] == "train"], "val": [q for q in qs if q["split"] == "val"]}


def load_test() -> list[dict]:
    """Held-out test questions. Call this only after the optimizer has finished (main/report.py)."""
    qs = _load("test.yml")
    for q in qs:
        q["reference"] = reference_value(q["sql"])
    return qs


def reference_value(sql: str) -> float:
    value, error = run_sql(sql)
    if error:
        raise ValueError(f"reference SQL does not run: {sql}: {error}")
    return value


def run_sql(sql: str) -> tuple[float | None, str | None]:
    """Run one query the way the app would: one read-only SELECT that returns one number.

    Returns (value, None) or (None, error message). The error message is what the metric puts in its feedback.
    """
    with _lock:
        try:
            statements = _con.extract_statements(sql)
        except duckdb.Error as e:
            return None, f"DuckDB could not parse it: {str(e).splitlines()[0]}"
        if len(statements) != 1:
            return None, f"{len(statements)} statements; the app runs exactly one"
        if statements[0].type != duckdb.StatementType.SELECT:
            return None, f"a {statements[0].type.name} statement; the app runs only SELECT"
        try:
            rows = _con.execute(sql).fetchall()
        except duckdb.Error as e:
            return None, f"DuckDB refused it: {str(e).splitlines()[0]}"
    if len(rows) != 1 or len(rows[0]) != 1:
        return None, f"{len(rows)} rows of {len(rows[0]) if rows else 0} columns; the app needs one row, one column"
    value = rows[0][0]
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        try:
            value = float(value)
        except (TypeError, ValueError):
            return None, f"the result {value!r} is not a number"
    return float(value), None

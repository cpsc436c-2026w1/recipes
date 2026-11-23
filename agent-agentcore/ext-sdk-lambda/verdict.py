"""From the model's TripAnswer and the tool log to the answer the analyst gets (README, Requirements R110-R113).

Checked in code, with no second model call, in this order:
  value is null                                     -> did_not_answer, no_value, with the model's reply
  value is not a cell of a successful run_sql,      -> did_not_answer, value_not_in_result
  nor derived from the question's results (below)
  (a whole number exactly, any other within 0.01)
  the reply does not state the value, or holds a    -> did_not_answer, reply_disagrees
  number of 10 or more that no query result, the
  question or the SQL of its run_sql calls holds
  otherwise                                         -> answered, with the SQL and rows of the latest query whose
                                                       result holds the value, or of the queries it is derived from

Derived from the question's results (`derived` names how): the row count of one complete result, the total of one
numeric column of one complete result, or the sum or difference of two numeric cells of the question's successful
results (the two counts of two zones, from one query or from two). A live run withheld a correct 593 that the model
computed from the counts 7661 and 7068 it had queried. Every operand comes from the tool log, so a number the model
invents is still withheld; a complete result is one not cut at the 50-row limit.
"""
from __future__ import annotations

import math
import re

TOLERANCE = 0.01   # a value that is not whole matches a cell within this (money and miles to the cent)
SMALL = 10         # numbers under this in a reply are not checked: "1 zone", "08:00", "3 zones"
NUMBER = re.compile(r"(?<![\w.])\d{1,3}(?:,\d{3})+(?:\.\d+)?|(?<![\w.,])\d+(?:\.\d+)?")


def numbers_in(text: str) -> list[float]:
    """Every number written in a text, thousands separators allowed: "19,074" and "19074" are both 19074."""
    return [float(m.group(0).replace(",", "")) for m in NUMBER.finditer(text or "")]


def is_whole(v: float) -> bool:
    return float(v).is_integer()


def same(cell, value: float) -> bool:
    """A cell holds the value: a whole number exactly, any other within TOLERANCE."""
    if isinstance(cell, bool) or not isinstance(cell, (int, float)) or (isinstance(cell, float) and math.isnan(cell)):
        return False
    if is_whole(value):
        return float(cell) == float(value)
    return abs(float(cell) - float(value)) <= TOLERANCE


def cells(entry: dict) -> list:
    return [v for row in entry.get("rows", []) for v in row]


def states(reply: str, value: float) -> bool:
    """The reply writes the value: its digits (with or without separators), or to 2 decimals when it is not whole."""
    if is_whole(value):
        return any(n == float(value) for n in numbers_in(reply))
    return any(round(n, 2) == round(float(value), 2) for n in numbers_in(reply))


def stray_numbers(reply: str, value: float, question: str, log: list[dict]) -> list[float]:
    """Numbers of 10 or more in the reply that are not the value and that no result, question or SQL holds."""
    known = [c for e in log if e["status"] == "ok" for c in cells(e)]
    known += numbers_in(question) + [n for e in log for n in numbers_in(e["sql"])]
    out = []
    for n in numbers_in(reply):
        if n < SMALL or same(n, value) or any(same(k, n) for k in known):
            continue
        out.append(n)
    return out


def _numeric(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and not (isinstance(v, float) and math.isnan(v))


def derivation(value: float, log: list[dict]) -> tuple[str, list[dict]] | None:
    """How the value follows from the question's results (see the module text), with the entries it uses; or None."""
    ok = [e for e in log if e["status"] == "ok"]
    for e in reversed(ok):
        if e.get("truncated"):
            continue
        if same(len(e["rows"]), value):
            return f"row count of the result ({len(e['rows'])} rows)", [e]
        for i, col in enumerate(e["columns"]):
            column = [r[i] for r in e["rows"]]
            if len(column) > 1 and all(_numeric(v) for v in column) and same(sum(column), value):
                return f"total of column {col}", [e]
    found = [(c, n, e) for n, e in enumerate(ok) for c in cells(e) if _numeric(c)]
    for a in range(len(found)):
        for b in range(len(found)):
            if a == b:
                continue
            (x, i, ex), (y, j, ey) = found[a], found[b]
            uses = [ex] if i == j else [ok[k] for k in sorted({i, j})]
            if a < b and same(x + y, value):
                return f"sum of {x:g} and {y:g}", uses
            if same(x - y, value):
                return f"difference of {x:g} and {y:g}", uses
    return None


def verdict(output, question: str, log: list[dict]) -> dict:
    """The answer's fields, from the model's TripAnswer (`output`) and the tool log."""
    reply, value = output.reply, output.value
    if value is None:
        return {"status": "did_not_answer", "reason": "no_value", "reply": reply, "value": None}
    holding = [e for e in log if e["status"] == "ok" and any(same(c, value) for c in cells(e))]
    derived, used = (None, holding[-1:]) if holding else (derivation(value, log) or (None, []))
    if not used:
        return {"status": "did_not_answer", "reason": "value_not_in_result", "reply": reply, "value": None}
    if not states(reply, value) or stray_numbers(reply, value, question, log):
        return {"status": "did_not_answer", "reason": "reply_disagrees", "reply": reply, "value": None}
    shown = int(value) if is_whole(value) else value
    return {"status": "answered", "reason": None, "reply": reply, "value": shown, "derived": derived,
            "sql": "\n".join(e["sql"] for e in used),
            "rows": [dict(zip(e["columns"], r)) for e in used for r in e["rows"]]}

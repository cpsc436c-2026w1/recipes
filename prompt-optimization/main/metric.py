"""The metric: a score for one answer, and a feedback text that names the failure type.

Decision (README, Decisions: "Metric with feedback"): an eval score alone says how often the step fails.
GEPA's reflection model also needs to know how it failed, so the metric returns a sentence per answer:
what was asked, what came back, and one of four failure types.

    pass         the query's value matches the reference value
    SQL error    the query does not parse, is not one SELECT, or does not return one number
    wrong unit   the value matches the reference after x100 or /60: a share for a percentage, seconds for minutes
    wrong value  any other mismatch
"""

from __future__ import annotations

from dataclasses import dataclass

import dspy

from main.data import run_sql

REL_TOL = 1e-6  # the cafe data is exact; widen this for data where rounding differs between correct queries


@dataclass
class Verdict:
    score: float      # 1.0 pass, 0.0 fail
    kind: str         # pass, SQL error, wrong unit, wrong value, no reply
    value: float | None
    feedback: str


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= REL_TOL * max(1.0, abs(b))


def check(q: dict, sql: str | None) -> Verdict:
    """Score one answer to question `q` (a dict with question, sql, reference)."""
    ref = q["reference"]
    # The feedback shows the reference SQL. GEPA reads feedback only for train questions; for validation
    # questions it reads only the score, and it never sees the test questions (README, Decisions: "Three splits").
    head = f"Question: {q['question']}\nReference SQL: {q['sql']}\nReference value: {ref:.4f}\n"
    if not sql or not sql.strip():
        return Verdict(0.0, "no reply", None, head + "FAILURE (no reply): the reply had no SQL.")
    value, error = run_sql(sql)
    if error:
        return Verdict(0.0, "SQL error", None,
                       head + f"Model SQL: {sql}\nFAILURE (SQL error): {error}.")
    if _close(value, ref):
        return Verdict(1.0, "pass", value, head + f"Model SQL: {sql}\nPASS: the value matches.")
    for factor, why in ((100.0, "a share from 0 to 1 where a percentage was asked"),
                        (1 / 60, "seconds where minutes were asked")):
        if _close(value * factor, ref):
            return Verdict(0.0, "wrong unit", value,
                           head + f"Model SQL: {sql}\nFAILURE (wrong unit): the query returned {value:.4f}, {why}.")
    return Verdict(0.0, "wrong value", value,
                   head + f"Model SQL: {sql}\nFAILURE (wrong value): the query returned {value:.4f}.")


def gepa_metric(gold, pred, trace=None, pred_name=None, pred_trace=None) -> dspy.Prediction:
    """The metric in the form dspy.GEPA calls it: five arguments, a score and a feedback text back."""
    v = check(gold.toDict() if hasattr(gold, "toDict") else gold, getattr(pred, "sql", None))
    return dspy.Prediction(score=v.score, feedback=v.feedback)

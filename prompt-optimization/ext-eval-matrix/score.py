"""The metric of the eval matrix with groups: one outcome per run, `score(case, answer) -> outcome`.

    correct           the case has a reference, and the answer's value equals it: a reference that is a whole
                      number exactly (19,075 against 19,074 is wrong), any other within 0.01 absolute (fares are in
                      dollars and cents)
    correct refusal   the case has no reference (`answerable: false`) and the step returned CANNOT_ANSWER
    wrong             anything else: a wrong value, an SQL error, a query for a case that has no reference, or
                      CANNOT_ANSWER for one that has (shown as refused, within wrong)

Decision (README, Decisions: "Whole numbers exactly, other values within 0.01"): `main/metric.py` compares with a
relative tolerance, which suits the cafe's exact data. A count is right or wrong, and two correct fare queries
differ only below the cent.

Two metrics, chosen by the caller: `value` (a step that returns one query, this recipe's) and `citation-set` (a step
that returns the ids of the documents it cites).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

REFUSAL = "CANNOT_ANSWER"
CENT = 0.01


@dataclass
class Outcome:
    outcome: str            # correct, correct refusal, wrong
    kind: str               # pass, SQL error, wrong value, no reply, refused an answerable case, answered an unanswerable case
    refused: bool           # the step returned CANNOT_ANSWER
    value: float | None = None

    @property
    def passed(self) -> bool:
        return self.outcome != "wrong"


def is_refusal(reply: str | None) -> bool:
    text = re.sub(r"^```[a-z]*\s*|\s*```$", "", (reply or "").strip()).strip().rstrip(".").strip()
    return text == REFUSAL


def value_matches(value: float, reference: float) -> bool:
    if float(reference).is_integer():
        return float(value) == float(reference)
    return abs(float(value) - float(reference)) <= CENT + 1e-9


def score_value(case: dict, reply: str | None, run_sql) -> Outcome:
    """Score one reply of the SQL step. `run_sql(sql) -> (value, error)` runs the query on the case's table."""
    answerable = bool(case["answerable"])
    if reply is None or not reply.strip():
        return Outcome("wrong", "no reply", False)
    if is_refusal(reply):
        if answerable:
            return Outcome("wrong", "refused an answerable case", True)
        return Outcome("correct refusal", "pass", True)
    value, error = run_sql(reply)
    if not answerable:
        return Outcome("wrong", "answered an unanswerable case", False, value)
    if error:
        return Outcome("wrong", "SQL error", False)
    if value_matches(value, case["reference"]):
        return Outcome("correct", "pass", False, value)
    return Outcome("wrong", "wrong value", False, value)


def score_citation_set(case: dict, cited: list[str] | str | None) -> Outcome:
    """Score one reply of a step that cites documents: `cited` is the list of ids, or CANNOT_ANSWER.

    A case lists `reference_ids` (the documents that support its answer) and `required_ids` (those a correct answer
    must cite, a subset of `reference_ids`, one at least). A run is correct when it cites at least one id, every
    cited id is a reference id, and every required id is cited. A case without `reference_ids` is unanswerable."""
    refs = set(case.get("reference_ids") or [])
    if isinstance(cited, str) or cited is None:
        refused = is_refusal(cited if isinstance(cited, str) else None)
        if refused and not refs:
            return Outcome("correct refusal", "pass", True)
        return Outcome("wrong", "refused an answerable case" if refused else "no reply", refused)
    ids = set(cited)
    if not refs:
        return Outcome("wrong", "answered an unanswerable case", False)
    required = set(case.get("required_ids") or [])
    if ids and ids <= refs and required <= ids:
        return Outcome("correct", "pass", False)
    if not ids:
        return Outcome("wrong", "no citation", False)
    return Outcome("wrong", "cites a document outside the references" if ids - refs else "a required document left out",
                   False)


def score(case: dict, answer, metric: str = "value", run_sql=None) -> Outcome:
    if metric == "value":
        return score_value(case, answer, run_sql)
    if metric == "citation-set":
        return score_citation_set(case, answer)
    raise ValueError(f"unknown metric {metric!r}: value or citation-set")

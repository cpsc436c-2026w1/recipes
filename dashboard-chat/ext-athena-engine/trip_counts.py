"""The eval trip-counts (R190): 10 analyst questions, each with a reference query whose rows are the answer.

A run is correct when (checked by code) its status is "answered", the rows of the last query that succeeded equal the
reference query's rows as a multiset (any order; the header row dropped and each value cast by its column type in
ResultSetMetadata, as query_fn.py does), and, when the answer is one number, the reply states that number. Each case
runs 3 times; a case passes when 2 of its 3 runs are correct. The eval passes when at least 24 of the 30 runs and at
least 8 of the 10 cases are correct. It is a regression check over 10 cases, not an accuracy estimate.

The reference rows are computed by running reference_sql on the same engine and data as the runs (Athena live; the
stand-in offline), so no number is written into the cases.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

CASES = Path(__file__).resolve().parent / "evals" / "trip_counts" / "cases.jsonl"
RUNS_PER_CASE = 3
MIN_RUNS, MIN_CASES = 24, 8


def load_cases(path: Path = CASES) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _key(row) -> tuple:
    return tuple(round(v, 6) if isinstance(v, float) else v for v in row)


def same_rows(expected: list[list], got: list[list]) -> bool:
    """Equal as multisets of typed rows, in any order."""
    return Counter(_key(r) for r in expected) == Counter(_key(r) for r in got)


def _numbers(text: str) -> set[float]:
    return {float(n.replace(",", "")) for n in re.findall(r"\d[\d,]*(?:\.\d+)?", text or "")}


def score(reference_rows: list[list], status: str, rows: list[list] | None, reply: str) -> tuple[bool, str]:
    if status != "answered":
        return False, f"status {status}"
    if rows is None:
        return False, "no query succeeded"
    if not same_rows(reference_rows, rows):
        return False, "rows differ"
    if len(reference_rows) == 1 and len(reference_rows[0]) == 1 and isinstance(reference_rows[0][0], (int, float)):
        if float(reference_rows[0][0]) not in _numbers(reply):
            return False, "the reply's number differs from the rows"
    return True, "correct"


def verdict(results: list[dict]) -> dict:
    by_case: dict[str, list[bool]] = {}
    for r in results:
        by_case.setdefault(r["case"], []).append(r["correct"])
    runs_ok = sum(r["correct"] for r in results)
    cases_ok = sum(sum(v) >= 2 for v in by_case.values())
    return {"runs": len(results), "runs_correct": runs_ok, "cases": len(by_case), "cases_passed": cases_ok,
            "passed": runs_ok >= MIN_RUNS and cases_ok >= MIN_CASES}


def run_eval(ask_one, reference, cases: list[dict], runs: int = RUNS_PER_CASE, log=print) -> dict:
    """ask_one(question) -> (status, last succeeded rows, reply); reference(sql) -> typed rows."""
    results = []
    for case in cases:
        ref = reference(case["reference_sql"])
        for n in range(1, runs + 1):
            status, rows, reply = ask_one(case["question"])
            ok, why = score(ref, status, rows, reply)
            results.append({"case": case["id"], "run": n, "correct": ok, "why": why})
            log(json.dumps(results[-1]))
    v = verdict(results)
    log(f"trip-counts: runs correct {v['runs_correct']}/{v['runs']}, cases passed {v['cases_passed']}/{v['cases']}; "
        f"{'PASS' if v['passed'] else 'FAIL'} (needs {MIN_RUNS} runs and {MIN_CASES} cases)")
    return {**v, "results": results}

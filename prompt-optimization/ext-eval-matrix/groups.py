"""Outcomes per group, the paired test of two cells, and the cost and time of a cell.

Reads the per-run results a matrix run writes to `runs/<time>/cases.jsonl` (one line per cell, case and run) and
never calls a model (README, Decisions: "The comparison reads saved results").

    a run      is `correct`, `correct refusal` or `wrong` (score.py)
    a case     passes when at least 2 of its 3 runs pass (`runs_needed`); its runs share its question and its
               difficulty, so they are not independent tests (README, Decisions: "Count each case once")
    a pair     two cells on the same cases: the exact McNemar test on the cases where they differ, for each group
               and for all cases, and on all cases a descriptive interval from a bootstrap stratified by group
"""

from __future__ import annotations

import math
import statistics
from collections import Counter

OUTCOMES = ("correct", "correct refusal", "wrong")
TOO_FEW = 6           # the smallest count of discordant cases at which a two-sided p under 0.05 can happen
RESAMPLES = 10_000
ALL = "all"


def runs_needed(runs: int) -> int:
    """How many of a case's runs must pass for the case to pass: 2 of 3 (two thirds, rounded up, for other counts)."""
    return max(1, math.ceil(2 * runs / 3))


def case_outcomes(rows: list[dict]) -> list[dict]:
    """One line per case of one cell, in the order of first appearance: its group, its runs passed and its outcome.

    A case with a reference can only be `correct` or `wrong`, one without only `correct refusal` or `wrong`, so the
    outcome of 2 of its 3 runs is the case's. A `wrong` case is shown as refused when at least 2 of its runs
    returned CANNOT_ANSWER (an answerable case the step would not answer)."""
    by_case: dict[str, list[dict]] = {}
    for r in rows:
        by_case.setdefault(r["case"], []).append(r)
    out = []
    for case, runs in by_case.items():
        need = runs_needed(len(runs))
        passed = sum(r["outcome"] != "wrong" for r in runs)
        refused = sum(bool(r.get("refused")) for r in runs)
        answerable = bool(runs[0]["answerable"])
        if passed >= need:
            outcome = "correct" if answerable else "correct refusal"
        else:
            outcome = "wrong"
        out.append({"case": case, "group": runs[0]["group"], "answerable": answerable, "runs": len(runs),
                    "runs_passed": passed, "outcome": outcome, "passed": outcome != "wrong",
                    "refused": outcome == "wrong" and answerable and refused >= need})
    return out


def group_order(cases: list[dict]) -> list[str]:
    """The groups in the order of the test file (first appearance)."""
    return list(dict.fromkeys(c["group"] for c in cases))


def group_rows(cases: list[dict]) -> list[dict]:
    """For each group and for all cases: cases correct, correct refusals, wrong, of the wrong ones refused, passed."""
    rows = []
    for group in group_order(cases) + [ALL]:
        cs = [c for c in cases if group == ALL or c["group"] == group]
        n = Counter(c["outcome"] for c in cs)
        rows.append({"group": group, "correct": n["correct"], "correct refusal": n["correct refusal"],
                     "wrong": n["wrong"], "refused": sum(c["refused"] for c in cs),
                     "passed": sum(c["passed"] for c in cs), "cases": len(cs)})
    return rows


def group_table(cell: str, cases: list[dict]) -> str:
    lines = [f"### {cell}", "",
             "| group | correct | correct refusal | wrong | of them refused | cases passed |",
             "|---|---:|---:|---:|---:|---:|"]
    for r in group_rows(cases):
        lines.append(f"| {r['group']} | {r['correct']} | {r['correct refusal']} | {r['wrong']} | {r['refused']} | "
                     f"{r['passed']} of {r['cases']} |")
    return "\n".join(lines) + "\n"


# ---- the paired comparison --------------------------------------------------------------------------------------

def mcnemar_p(b: int, a: int) -> float:
    """The exact McNemar p: a two-sided binomial test of b out of a + b at one half. 1.0 when a + b = 0.

    Among the cases where the two cells differ, does one cell win more often than a coin would give? This is
    `scipy.stats.binomtest(b, a + b, 0.5).pvalue`; tests/test_groups.py checks the two agree."""
    n = a + b
    if n == 0:
        return 1.0
    from scipy.stats import binomtest
    return float(binomtest(b, n, 0.5).pvalue)


def _pairs(first: list[dict], second: list[dict]) -> list[tuple[dict, dict]]:
    """The cases of two cells, paired by case id, in the order of the first cell (the test file's order)."""
    other = {c["case"]: c for c in second}
    missing = [c["case"] for c in first if c["case"] not in other]
    extra = [c for c in other if c not in {x["case"] for x in first}]
    if missing or extra:
        raise ValueError(f"the two cells were not run on the same cases: {', '.join(missing + extra)}")
    return [(c, other[c["case"]]) for c in first]


def bootstrap_interval(pairs: list[tuple[dict, dict]], seed: int = 0, resamples: int = RESAMPLES) -> tuple[float, float]:
    """A descriptive 95% interval of the mean difference (B minus A) in each case's share of runs passed.

    Stratified by group: for each resample, for each group in the order of the test file, `rng.integers(0, n, n)`
    draws that group's n cases with replacement, the same draw for both cells. The 2.5th and 97.5th percentiles
    (NumPy's default, linear). The loop order is part of the method: the same seed gives the same draws only in
    this order (README, Decisions: "The bootstrap stratified by group")."""
    import numpy as np
    groups = group_order([a for a, _ in pairs])
    diff = np.array([b["runs_passed"] / b["runs"] - a["runs_passed"] / a["runs"] for a, b in pairs])
    index = [np.array([i for i, (a, _) in enumerate(pairs) if a["group"] == g]) for g in groups]
    rng = np.random.default_rng(seed)
    means = np.empty(resamples)
    for k in range(resamples):
        drawn = np.concatenate([ix[rng.integers(0, len(ix), len(ix))] for ix in index])
        means[k] = diff[drawn].mean()
    low, high = np.percentile(means, [2.5, 97.5])
    return float(low), float(high)


def compare(first: list[dict], second: list[dict], seed: int = 0, group_intervals: bool = False) -> dict:
    """Rows for each group and for all cases, and on all cases the difference in runs passed with its interval.

    `group_intervals` (off unless `--group-intervals`): a descriptive interval on each group row as well. Off by
    default: with 5 cases a percentile bootstrap leaves 0 out where the exact test does not (README, Decisions:
    "An exact test for each group"); whether to show it is the course staff's call."""
    pairs = _pairs(first, second)
    rows = []
    for group in group_order(first) + [ALL]:
        ps = [(a, b) for a, b in pairs if group == ALL or a["group"] == group]
        b_only = sum(b["passed"] and not a["passed"] for a, b in ps)
        a_only = sum(a["passed"] and not b["passed"] for a, b in ps)
        row = {"group": group, "cases": len(ps), "a_passed": sum(a["passed"] for a, _ in ps),
               "b_passed": sum(b["passed"] for _, b in ps), "b_only": b_only, "a_only": a_only,
               "difference": (sum(b["passed"] for _, b in ps) - sum(a["passed"] for a, _ in ps)) / len(ps),
               "p": mcnemar_p(b_only, a_only), "too_few": b_only + a_only < TOO_FEW}
        if group_intervals and group != ALL:
            row["interval"] = bootstrap_interval(ps, seed)
        rows.append(row)
    runs_diff = statistics.fmean(b["runs_passed"] / b["runs"] - a["runs_passed"] / a["runs"] for a, b in pairs)
    return {"rows": rows, "runs_difference": runs_diff, "interval": bootstrap_interval(pairs, seed), "seed": seed}


def comparison_table(name_a: str, name_b: str, result: dict) -> str:
    """Markdown: the comparison of two cells. Same results and seed, same text (R86)."""
    with_iv = any("interval" in r for r in result["rows"])
    head = (f"| group | {name_a} passed | {name_b} passed | {name_b} only | {name_a} only | difference | p | mark |"
            + (" interval (descriptive) |" if with_iv else ""))
    lines = [f"### {name_a} against {name_b}", "",
             f"B is `{name_b}`, A is `{name_a}`; the difference is the share of cases passed, B minus A; p is the "
             "exact McNemar p.", "", head, "|---|---:|---:|---:|---:|---:|---:|---|" + ("---|" if with_iv else "")]
    for r in result["rows"]:
        line = (f"| {r['group']} | {r['a_passed']} of {r['cases']} | {r['b_passed']} of {r['cases']} | "
                f"{r['b_only']} | {r['a_only']} | {r['difference']:+.2f} | {r['p']:.4f} | "
                f"{'too few cases' if r['too_few'] else ''} |")
        if with_iv:
            iv = r.get("interval")
            line += f" {iv[0]:+.2f} to {iv[1]:+.2f} |" if iv else " |"
        lines.append(line)
    low, high = result["interval"]
    lines += ["", f"All cases, difference in the share of runs passed (B minus A): {result['runs_difference']:+.2f}, "
                  f"descriptive 95% interval {low:+.2f} to {high:+.2f} (bootstrap stratified by group, "
                  f"{RESAMPLES:,} resamples, seed {result['seed']})."]
    return "\n".join(lines) + "\n"


# ---- cost and time ---------------------------------------------------------------------------------------------

def p95_nearest_rank(values: list[float]) -> float:
    """The 95th percentile by nearest rank: the smallest value with at least 95% of the values at or below it."""
    xs = sorted(values)
    return xs[max(1, math.ceil(0.95 * len(xs))) - 1]


def cost_time(rows: list[dict]) -> dict:
    """CAD per answer, and the median and p95 time of an answer (seconds), over every run of one cell."""
    seconds = [r["seconds"] for r in rows]
    cad = sum(r["cad"] for r in rows)
    return {"answers": len(rows), "cad": cad, "cad_per_answer": cad / len(rows) if rows else 0.0,
            "median_s": statistics.median(seconds) if seconds else 0.0,
            "p95_s": p95_nearest_rank(seconds) if seconds else 0.0}


def cost_time_table(cells: dict[str, list[dict]]) -> str:
    lines = ["| cell | answers | CAD | CAD per answer | median time (s) | p95 time (s) |",
             "|---|---:|---:|---:|---:|---:|"]
    for name, rows in cells.items():
        c = cost_time(rows)
        lines.append(f"| {name} | {c['answers']} | {c['cad']:.6f} | {c['cad_per_answer']:.6f} | "
                     f"{c['median_s']:.2f} | {c['p95_s']:.2f} |")
    return "\n".join(lines) + "\n"


# ---- the eval gate ---------------------------------------------------------------------------------------------
GATE = {"runs": 0.8, "cases": 0.8, "group": 0.6}   # eval trip-sql: 48 of 60 runs, 16 of 20 cases, 3 of 5 a group


def gate(rows: list[dict]) -> dict:
    """The eval gate of one cell: enough runs passed, enough cases passed, and enough cases in every group.

    A regression check over the test cases, not an accuracy estimate."""
    cases = case_outcomes(rows)
    need = {"runs": math.ceil(GATE["runs"] * len(rows) - 1e-9), "cases": math.ceil(GATE["cases"] * len(cases) - 1e-9)}
    runs_ok = sum(r["outcome"] != "wrong" for r in rows)
    cases_ok = sum(c["passed"] for c in cases)
    short = []
    for g in group_order(cases):
        cs = [c for c in cases if c["group"] == g]
        if sum(c["passed"] for c in cs) < math.ceil(GATE["group"] * len(cs) - 1e-9):
            short.append(f"{g} {sum(c['passed'] for c in cs)} of {len(cs)}")
    passed = runs_ok >= need["runs"] and cases_ok >= need["cases"] and not short
    return {"passed": passed, "runs": (runs_ok, len(rows), need["runs"]), "cases": (cases_ok, len(cases), need["cases"]),
            "short": short}


def gate_table(cells: dict[str, list[dict]]) -> str:
    lines = ["| cell | runs passed | cases passed | groups under 60% | gate |", "|---|---:|---:|---|---|"]
    for name, rows in cells.items():
        g = gate(rows)
        (r, rn, rneed), (c, cn, cneed) = g["runs"], g["cases"]
        lines.append(f"| {name} | {r} of {rn} (need {rneed}) | {c} of {cn} (need {cneed}) | "
                     f"{', '.join(g['short']) or 'none'} | {'passes' if g['passed'] else 'fails'} |")
    return "\n".join(lines) + "\n"


# ---- compare saved runs again ---------------------------------------------------------------------------------

def main(argv=None) -> str:
    """Compare two cells of a saved matrix run again, from its cases.jsonl, without a model call:

        uv run python ext-eval-matrix/groups.py runs/<time>/cases.jsonl --compare A,B [--seed 0]
    """
    import argparse
    import json
    from pathlib import Path
    ap = argparse.ArgumentParser()
    ap.add_argument("cases", type=Path, help="cases.jsonl of a matrix run")
    ap.add_argument("--compare", required=True, metavar="A,B")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--group-intervals", action="store_true")
    a = ap.parse_args(argv)
    rows = [json.loads(line) for line in a.cases.read_text().splitlines() if line.strip()]
    first, _, second = a.compare.partition(",")
    cells = {c: [r for r in rows if r["cell"] == c] for c in (first, second)}
    if not all(cells.values()):
        raise SystemExit(f"no runs of {', '.join(c for c, rs in cells.items() if not rs)} in {a.cases} "
                         f"(cells: {', '.join(dict.fromkeys(r['cell'] for r in rows))})")
    text = comparison_table(first, second, compare(case_outcomes(cells[first]), case_outcomes(cells[second]),
                                                   a.seed, a.group_intervals))
    print(text)
    return text


if __name__ == "__main__":
    main()

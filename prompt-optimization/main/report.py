"""The final report: both prompts on the held-out test questions, several runs each, pass rate and cost.

Decision (README, Decisions: "Report on test, several runs"): the validation score of the winner is not
reported, because the winner was chosen for being highest there. Model replies vary between runs, so each
prompt runs `runs` times and the report gives the median with every run.
"""

from __future__ import annotations

import statistics
from collections import Counter

import dspy

from main.metric import check
from main.models import Budget, BudgetExceeded, ModelCallFailed


def evaluate(answer, questions: list[dict], runs: int) -> list[dict]:
    """One row per answer: run number, question id, failure type, score.

    `answer(question) -> sql` is the step under test: a DSPy program (main/) or a plain prompt (ext-*/).
    """
    rows = []
    for run in range(1, runs + 1):
        for q in questions:
            try:
                sql = answer(q["question"])
            except BudgetExceeded:
                raise
            except Exception as err:
                failure = _call_failure(err)
                if failure is not None:  # no reply at all (token, rate limit, network): stop, do not score it
                    raise failure from None
                sql = None  # a reply that cannot be parsed counts as a failure, not a crash
            v = check(q, sql)
            rows.append({"run": run, "id": q["id"], "kind": v.kind, "score": v.score, "sql": sql})
    return rows


def _call_failure(err: BaseException | None) -> BaseException | None:
    """The refused or failed model call behind `err` (DSPy may wrap it), or None if there is none."""
    seen = set()
    while err is not None and id(err) not in seen:
        if isinstance(err, (ModelCallFailed, BudgetExceeded)):
            return err
        seen.add(id(err))
        err = err.__cause__ or err.__context__
    return None


def program_answer(program, lm: dspy.BaseLM):
    """`answer` for a DSPy program, calling `lm` with the ChatAdapter."""
    def answer(question: str) -> str:
        with dspy.context(lm=lm, adapter=dspy.ChatAdapter()):
            return program(question=question).sql
    return answer


def search_end(budget: Budget) -> str:
    """After the search: the line for the report that says what ended it, and at which call if it was cut.

    A refused or failed call cuts the search; the candidates found before it are kept and reported. A candidate
    whose validation run the failure hit may have a lower validation score than it would have had.
    """
    if budget.failed:
        why = f"model call {budget.cut_at} (every role counted) failed, so the search was cut there ({budget.failed})"
    elif budget.stopped_by.startswith("spend cap, call refused"):
        why = f"{budget.stopped_by}, model call {budget.cut_at} (every role counted) was refused and the search was cut there"
    else:
        why = budget.stopped_by or "the budget of metric calls (--max-metric-calls)"
    return f"\nThe search ended at: {why}\n"


def tests_cut(err: BaseException, done: list[str], prompts: list[str]) -> str:
    """The report section for test runs that a refused or failed call stopped: what ran and what did not."""
    missing = [p for p in prompts if p not in done]
    return (f"\n**The test runs stopped before they were done:** {err}. Not tested, or not finished: "
            f"{', '.join(missing)}. The table has a row only for a prompt whose runs all finished.\n")


def summary(rows: list[dict]) -> dict:
    runs = sorted({r["run"] for r in rows})
    per_run = [sum(r["score"] for r in rows if r["run"] == n) / sum(1 for r in rows if r["run"] == n) for n in runs]
    fails = Counter(r["kind"] for r in rows if r["kind"] != "pass")
    return {"median": statistics.median(per_run), "per_run": per_run,
            "passed": int(sum(r["score"] for r in rows)), "answers": len(rows), "failures": dict(fails)}


def per_question(results: dict[str, list[dict]], questions: list[dict]) -> str:
    """Markdown: for each test question, in how many of its runs each prompt passed.

    A median over runs can go up while one question goes from always right to always wrong. This table shows
    which questions the new prompt fixed, which it left, and which it broke."""
    names = list(results)
    if not names:
        return ""
    runs = {name: len({r["run"] for r in rows}) for name, rows in results.items()}
    lines = ["", "## Per question (runs passed, out of " + " and ".join(f"{runs[n]} for {n}" for n in names) + ")",
             "", "| id | question | " + " | ".join(names) + " |", "|---|---|" + "---:|" * len(names)]
    passed = {name: {} for name in names}
    for name, rows in results.items():
        for r in rows:
            passed[name][r["id"]] = passed[name].get(r["id"], 0) + int(r["score"])
    for q in questions:
        lines.append(f"| {q['id']} | {q['question']} | " + " | ".join(str(passed[n].get(q["id"], 0)) for n in names)
                     + " |")
    if len(names) == 2:
        first, second = (passed[n] for n in names)
        more = sum(second.get(q["id"], 0) > first.get(q["id"], 0) for q in questions)
        less = sum(second.get(q["id"], 0) < first.get(q["id"], 0) for q in questions)
        lines += ["", f"With the prompt `{names[1]}`, {more} of {len(questions)} questions pass more often than with "
                      f"`{names[0]}`, {less} less often, {len(questions) - more - less} the same."]
    return "\n".join(lines) + "\n"


def table(results: dict[str, list[dict]], budget: Budget, label: str) -> str:
    """Markdown: one row per prompt on the test questions, then the spend by role."""
    pct = lambda x: f"{100 * x:.0f}%"  # noqa: E731
    lines = [f"## Test pass rate ({label})", "",
             "| prompt | median | per run | answers passed | failures | test cost (CAD) |",
             "|---|---:|---|---:|---|---:|"]
    for name, rows in results.items():
        s = summary(rows)
        fails = ", ".join(f"{k} {v}" for k, v in sorted(s["failures"].items())) or "none"
        lines.append(f"| {name} | {pct(s['median'])} | {' / '.join(pct(x) for x in s['per_run'])} | "
                     f"{s['passed']}/{s['answers']} | {fails} | {budget.cad.get('test-' + name, 0):.6f} |")
    lines += ["", "## Spend", "", "| role | calls | tokens in | tokens out | CAD |", "|---|---:|---:|---:|---:|"]
    for role in sorted(budget.calls):
        t_in, t_out = budget.tokens.get(role, [0, 0])
        lines.append(f"| {role} | {budget.calls[role]} | {t_in:,} | {t_out:,} | {budget.cad.get(role, 0):.6f} |")
    lines.append(f"| total | {sum(budget.calls.values())} | | | {budget.total_cad():.6f} |")
    return "\n".join(lines) + "\n"

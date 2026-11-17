"""The main scenario end to end: optimize on train and validation, then report on test.

    uv run python -m main.run                          # offline, stub models, no key
    uv run python -m main.run --target gateway         # the course LLM gateway: COURSE_GATEWAY_URL, KB_MCP_TOKEN

Writes runs/<time>/: report.md, instructions_before.txt, instructions_after.txt, candidates.json, program.json,
answers.json, spend.json and run.json (what produced the new instructions).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import time
from importlib.metadata import version
from pathlib import Path

from main.data import load_dev, load_test
from main.models import (REFLECTION_MODEL, TASK_MODEL, Budget, BudgetExceeded, ModelCallFailed, make_lm,
                         test_allowance)
from main.optimize import optimize
from main.program import SEED_INSTRUCTIONS, SQLAnswerer, instructions_of
from main.report import evaluate, per_question, program_answer, search_end, summary, table, tests_cut

ROOT = Path(__file__).resolve().parents[1]


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def manifest(a, offline: bool, best_instructions: str, ended: str, results: dict) -> dict:
    """What produced this prompt, so that the instructions in your app can be traced to the run that made them:
    the models, the package versions, the question files and the seed by their hash, the caps, what ended the
    search, and the test medians. No key and no question text."""
    return {
        "written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "target": "stub" if offline else "gateway",
        "task_model": a.task_model, "reflection_model": a.reflection_model,
        "packages": {"dspy": version("dspy"), "gepa": version("gepa")},
        "sha256_16": {"data/questions.yml": _sha((ROOT / "data" / "questions.yml").read_text()),
                      "data/test.yml": _sha((ROOT / "data" / "test.yml").read_text()),
                      "instructions_before": _sha(SEED_INSTRUCTIONS + "\n"),
                      "instructions_after": _sha(best_instructions + "\n")},
        "caps": {"max_metric_calls": a.max_metric_calls, "max_reflection_calls": a.max_reflection_calls,
                 "max_test_calls": a.max_test_calls, "max_cad": a.max_cad, "runs": a.runs, "seed": a.seed},
        "search_ended": ended.strip().removeprefix("The search ended at: "),
        "test_median": {name: summary(rows)["median"] for name, rows in results.items()},
    }


def main(argv=None) -> Path:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", choices=["stub", "gateway"], default="stub")
    ap.add_argument("--task-model", default=TASK_MODEL)
    ap.add_argument("--reflection-model", default=REFLECTION_MODEL)
    # Spend cap: the limits below. The search and the test runs each have their own count of calls; the money
    # cap is one for the whole run, and the search stops at 60% of it.
    ap.add_argument("--max-metric-calls", type=int, default=120, help="GEPA's task-model calls on train and val")
    ap.add_argument("--max-reflection-calls", type=int, default=8)
    ap.add_argument("--max-test-calls", type=int, default=None,
                    help="calls for the test runs, apart from the search's (default: 2 x 2 prompts x runs x "
                         "test questions)")
    ap.add_argument("--max-cad", type=float, default=0.50, help="stop once this many Canadian dollars are spent")
    ap.add_argument("--runs", type=int, default=3, help="test runs per prompt")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--verbose", action="store_true", help="print GEPA's log of every iteration")
    a = ap.parse_args(argv)
    offline = a.target == "stub"
    logging.getLogger("dspy").setLevel(logging.INFO if a.verbose else logging.WARNING)

    dev = load_dev()
    # The search's hard cap: GEPA checks its count of metric calls between iterations, so one iteration (the
    # current and the new candidate on 3 train questions each, then every validation question) may pass it.
    budget = Budget(max_reflection_calls=a.max_reflection_calls, max_cad=a.max_cad,
                    max_task_calls=a.max_metric_calls + 2 * 3 + len(dev["val"]))

    # Step 1: GEPA on train (reflection) and validation (selection). The test questions are not loaded yet.
    task_lm = make_lm(a.task_model, "task", budget, offline)
    reflection_lm = make_lm(a.reflection_model, "reflect", budget, offline)
    try:
        best, candidates = optimize(dev["train"], dev["val"], task_lm, reflection_lm, budget,
                                    max_metric_calls=a.max_metric_calls, seed=a.seed)
    except (BudgetExceeded, ModelCallFailed) as err:
        raise SystemExit(f"the search stopped before it had a result, so nothing was written: {err}") from None
    ended = search_end(budget)  # read before the test calls: what ended the search, and where it was cut

    # The search result is written first: a test run that is refused or fails does not lose it.
    out = a.out or ROOT / "runs" / time.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    (out / "instructions_before.txt").write_text(SEED_INSTRUCTIONS + "\n")
    (out / "instructions_after.txt").write_text(instructions_of(best) + "\n")
    (out / "candidates.json").write_text(json.dumps(candidates, indent=1))
    best.save(str(out / "program.json"))

    # Step 2: only now load the test questions, and run both prompts on them.
    test = load_test()
    budget.max_test_calls = a.max_test_calls = a.max_test_calls or test_allowance(a.runs, len(test))
    results, cut = {}, None
    try:
        for name, program in (("before", SQLAnswerer()), ("after", best)):
            lm = make_lm(a.task_model, f"test-{name}", budget, offline)
            results[name] = evaluate(program_answer(program, lm), test, runs=a.runs)
    except (BudgetExceeded, ModelCallFailed) as err:
        cut = err

    label = f"{len(test)} questions, {a.runs} runs per prompt, task model {a.task_model}"
    label += ", offline stub" if offline else ", course LLM gateway"
    report = f"# Prompt optimization: {a.task_model}{' (stub)' if offline else ''}\n\n" + table(results, budget, label)
    if cut is not None:
        report += tests_cut(cut, list(results), ["before", "after"])
    report += per_question(results, test)
    report += "\n## Candidates (validation score, used only to choose)\n\n"
    report += "".join(f"- #{c['idx']}: {100 * c['val_score']:.0f}%\n" for c in candidates) + ended
    (out / "report.md").write_text(report)
    (out / "spend.json").write_text(json.dumps({"calls": budget.calls, "tokens": budget.tokens, "cad": budget.cad,
                                                 "held": round(budget.held, 9)}))
    (out / "answers.json").write_text(json.dumps(results, indent=1))
    (out / "run.json").write_text(json.dumps(manifest(a, offline, instructions_of(best), ended, results), indent=1))
    if cut is not None:
        raise SystemExit(f"the test runs stopped: {cut}. The search result and a report of what ran are in {out}. "
                         "Raise --max-cad or --max-test-calls, or lower --runs.")
    print(report)
    print(f"written to {out}")
    return out


if __name__ == "__main__":
    main()

"""The main scenario without DSPy: the `gepa` library optimizes an existing system prompt string directly.

    uv run python ext-gepa-direct/optimize_direct.py                     # offline, stub models
    uv run python ext-gepa-direct/optimize_direct.py --target gateway    # the course LLM gateway

This is how the L15 demo ran GEPA (evals/gepa/optimize.py in l15-genai-querychat): the text being optimized is
the very system prompt the app sends, through the app's own call. What changes from main/ (ext-gepa-direct/README.md):
the program is a prompt string and one chat call instead of a DSPy signature; GEPA needs an adapter with two
methods instead of a metric with five arguments. The data, the splits, the metric and the spend cap are main/'s.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # this folder name has a hyphen, so it runs as a script and imports main/ by path

from gepa import optimize  # noqa: E402
from gepa.core.adapter import EvaluationBatch  # noqa: E402

from main.data import SCHEMA, load_dev, load_test  # noqa: E402
from main.metric import check  # noqa: E402
from main.models import (REFLECTION_MODEL, TASK_MODEL, Budget, BudgetExceeded, ModelCallFailed,  # noqa: E402
                         gateway_chat, stub_chat, test_allowance)
from main.report import per_question, evaluate, search_end, table, tests_cut  # noqa: E402

# The system prompt in two parts, as in the L15 demo: HEAD stays as written, GEPA rewrites only INSTRUCTIONS.
HEAD = f"You write SQL for a cafe's reporting app.\n\n<tables>\n{SCHEMA}\n</tables>\n\n"
SEED_INSTRUCTIONS = ("Answer the question with one DuckDB SQL query over the tables above. "
                     "The app runs the query and shows its single value to the user. Reply with the query.")


def render(instructions: str) -> str:
    return HEAD + instructions


def extract_sql(reply: str) -> str:
    """The query from a reply: the inside of a ```sql block if there is one, else the whole reply."""
    m = re.search(r"```(?:sql)?\s*\n(.*?)```", reply, flags=re.S | re.I)
    return (m.group(1) if m else reply).strip()


class PromptAdapter:
    """GEPA's adapter: run a batch with one candidate prompt, and turn the results into reflection examples."""

    def __init__(self, chat, model: str, budget: Budget):
        self.chat, self.model, self.budget = chat, model, budget
        self.propose_new_texts = None  # None: GEPA's own reflection prompt and proposal

    def evaluate(self, batch, candidate, capture_traces=False):
        recs = []
        for q in batch:
            sql = extract_sql(self.chat(self.model, render(candidate["instructions"]), q["question"],
                                        "task", self.budget))
            v = check(q, sql)
            recs.append({"question": q["question"], "sql": sql, "score": v.score, "feedback": v.feedback})
        return EvaluationBatch(outputs=[r["sql"] for r in recs], scores=[r["score"] for r in recs],
                               trajectories=recs if capture_traces else None)

    def make_reflective_dataset(self, candidate, eval_batch, components_to_update):
        # What the reflection model reads for each train question: the input, the output and the metric's feedback.
        items = [{"Inputs": {"question": r["question"]}, "Generated Outputs": {"sql": r["sql"]},
                  "Feedback": r["feedback"]} for r in eval_batch.trajectories]
        return {c: items for c in components_to_update}


class _Quiet:
    def log(self, *args, **kwargs):
        pass


def main(argv=None) -> Path:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", choices=["stub", "gateway"], default="stub")
    ap.add_argument("--task-model", default=TASK_MODEL)
    ap.add_argument("--reflection-model", default=REFLECTION_MODEL)
    ap.add_argument("--max-metric-calls", type=int, default=120)
    ap.add_argument("--max-reflection-calls", type=int, default=8)
    ap.add_argument("--max-test-calls", type=int, default=None,
                    help="calls for the test runs, apart from the search's (default: 2 x 2 prompts x runs x "
                         "test questions)")
    ap.add_argument("--max-cad", type=float, default=0.50)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--verbose", action="store_true", help="print GEPA's log of every iteration")
    a = ap.parse_args(argv)
    chat = stub_chat if a.target == "stub" else gateway_chat

    dev = load_dev()  # train and validation only; the test split is loaded after GEPA has finished
    # The search's hard cap, as in main/run.py: GEPA may pass max_metric_calls by one iteration.
    budget = Budget(max_reflection_calls=a.max_reflection_calls, max_cad=a.max_cad,
                    max_task_calls=a.max_metric_calls + 2 * 3 + len(dev["val"]))
    try:
        res = optimize(
            seed_candidate={"instructions": SEED_INSTRUCTIONS},
            trainset=dev["train"], valset=dev["val"],
            adapter=PromptAdapter(chat, a.task_model, budget),
            reflection_lm=lambda prompt: chat(a.reflection_model, "", prompt, "reflect", budget),
            candidate_selection_strategy="pareto", reflection_minibatch_size=3, use_merge=False,
            # Two stop conditions, both checked before every iteration (GEPAEngine._should_stop, gepa/core/engine.py):
            # GEPA's own count of metric calls, and budget.stop. With raise_on_exception=False the engine logs an
            # exception from evaluate() and goes to the next iteration; a call refused by the cap raises on every
            # iteration and adds no metric calls, so only budget.stop (which sees the refusal) ends that loop.
            max_metric_calls=a.max_metric_calls, stop_callbacks=[budget.stop],
            seed=a.seed, raise_on_exception=False, display_progress_bar=False,
            logger=None if a.verbose else _Quiet(),
        )
    except (BudgetExceeded, ModelCallFailed) as err:
        # The seed's validation run is outside the loop: a refused or failed call there leaves no result.
        raise SystemExit(f"the search stopped before it had a result, so nothing was written: {err}") from None
    ended = search_end(budget)  # read before the test calls: what ended the search, and where it was cut
    best = res.best_candidate["instructions"]

    # The search result is written first: a test run that is refused or fails does not lose it.
    out = a.out or ROOT / "runs" / ("direct-" + time.strftime("%Y%m%d-%H%M%S"))
    out.mkdir(parents=True, exist_ok=True)
    (out / "instructions_after.txt").write_text(best + "\n")
    (out / "candidates.json").write_text(json.dumps(
        [{"idx": i, "val_score": s, "instructions": c["instructions"]}
         for i, (c, s) in enumerate(zip(res.candidates, res.val_aggregate_scores))], indent=1))

    test = load_test()
    budget.max_test_calls = a.max_test_calls or test_allowance(a.runs, len(test))
    results, cut = {}, None
    try:
        for name, instr in (("before", SEED_INSTRUCTIONS), ("after", best)):
            role = f"test-{name}"
            results[name] = evaluate(lambda question, i=instr, r=role: extract_sql(
                chat(a.task_model, render(i), question, r, budget)), test, runs=a.runs)
    except (BudgetExceeded, ModelCallFailed) as err:
        cut = err

    label = f"{len(test)} questions, {a.runs} runs per prompt, gepa library directly, task model {a.task_model}"
    label += ", offline stub" if a.target == "stub" else ", course LLM gateway"
    report = "# Prompt optimization with the gepa library\n\n" + table(results, budget, label)
    if cut is not None:
        report += tests_cut(cut, list(results), ["before", "after"])
    report += per_question(results, test)
    report += "\n## Candidates (validation score, used only to choose)\n\n"
    report += "".join(f"- #{i}: {100 * s:.0f}%\n" for i, s in enumerate(res.val_aggregate_scores)) + ended
    (out / "report.md").write_text(report)
    (out / "spend.json").write_text(json.dumps({"calls": budget.calls, "tokens": budget.tokens, "cad": budget.cad,
                                                 "held": round(budget.held, 9)}))
    if cut is not None:
        raise SystemExit(f"the test runs stopped: {cut}. The search result and a report of what ran are in {out}. "
                         "Raise --max-cad or --max-test-calls, or lower --runs.")
    print(report)
    print(f"written to {out}")
    return out


if __name__ == "__main__":
    main()

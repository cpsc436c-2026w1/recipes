"""The same eval over several models and prompts: pass rate and cost per cell, on the test questions.

    uv run python ext-eval-matrix/matrix.py --prompt after=runs/<time>/instructions_after.txt
    uv run python ext-eval-matrix/matrix.py --target gateway --models qwen3.6-35b-a3b,qwen3.8-27b \\
        --prompt after=runs/<time>/instructions_after.txt

What changes from main/ (ext-eval-matrix/README.md): no optimization step. Each cell is one model with one prompt,
run `--runs` times on the test questions with main/'s program, metric and spend cap. The seed prompt is always a row.
The question a matrix answers: which is the cheapest cell that passes often enough for your feature?
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # this folder name has a hyphen, so it runs as a script and imports main/ by path

from main.data import load_test  # noqa: E402
from main.models import REFLECTION_MODEL, TASK_MODEL, Budget, BudgetExceeded, ModelCallFailed, make_lm  # noqa: E402
from main.program import SEED_INSTRUCTIONS, SQLAnswerer  # noqa: E402
from main.report import evaluate, program_answer, summary  # noqa: E402


def matrix(models: list[str], prompts: dict[str, str], runs: int, offline: bool,
           budget: Budget) -> tuple[list[dict], str]:
    """The cells that finished, and "" or the reason the matrix stopped before its last cell.

    A call that a cap refuses, or that the endpoint refuses or fails, stops the matrix: the cells that finished are
    still reported, and the cell that was cut is not (a row from part of its runs would not be comparable)."""
    test = load_test()  # a matrix only measures; nothing here changes a prompt, so the test split is safe to use
    cells = []
    for model in models:
        for name, instructions in prompts.items():
            role = f"cell {model} / {name}"
            lm = make_lm(model, role, budget, offline)
            try:
                rows = evaluate(program_answer(SQLAnswerer(instructions), lm), test, runs=runs)
            except (BudgetExceeded, ModelCallFailed) as err:
                return cells, f"{model} with the prompt {name!r}: {err}"
            cells.append({"model": model + (" (stub)" if offline else ""), "prompt": name, **summary(rows),
                          "calls": budget.calls.get(role, 0), "cad": budget.cad.get(role, 0.0)})
    return cells, ""


def table(cells: list[dict], label: str) -> str:
    pct = lambda x: f"{100 * x:.0f}%"  # noqa: E731
    lines = [f"## Eval matrix ({label})", "",
             "| model | prompt | median | per run | answers passed | failures | CAD | CAD per 1,000 answers |",
             "|---|---|---:|---|---:|---|---:|---:|"]
    for c in cells:
        fails = ", ".join(f"{k} {v}" for k, v in sorted(c["failures"].items())) or "none"
        per_1000 = 1000 * c["cad"] / c["calls"] if c["calls"] else 0.0
        lines.append(f"| {c['model']} | {c['prompt']} | {pct(c['median'])} | "
                     f"{' / '.join(pct(x) for x in c['per_run'])} | {c['passed']}/{c['answers']} | {fails} | "
                     f"{c['cad']:.6f} | {per_1000:.4f} |")
    return "\n".join(lines) + "\n"


def main(argv=None) -> Path:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", choices=["stub", "gateway"], default="stub")
    ap.add_argument("--models", default=f"{TASK_MODEL},{REFLECTION_MODEL}", help="comma-separated model ids")
    ap.add_argument("--prompt", action="append", default=[], metavar="NAME=FILE",
                    help="a prompt to compare with the seed: instructions read from FILE; repeat for more")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--max-calls", type=int, default=300, help="spend cap: model calls for the whole matrix")
    ap.add_argument("--max-cad", type=float, default=0.50)
    ap.add_argument("--out", type=Path, default=None)
    a = ap.parse_args(argv)
    prompts = {"seed": SEED_INSTRUCTIONS}
    for item in a.prompt:
        name, _, path = item.partition("=")
        prompts[name] = Path(path).read_text().strip()
    models = [m for m in a.models.split(",") if m]
    budget = Budget(max_task_calls=a.max_calls, max_cad=a.max_cad)
    cells, cut = matrix(models, prompts, a.runs, a.target == "stub", budget)
    label = f"{a.runs} runs per cell" + (", offline stub" if a.target == "stub" else ", course LLM gateway")
    report = "# Eval matrix\n\n" + table(cells, label)
    if cut:
        done, planned = len(cells), len(models) * len(prompts)
        # The cut cell has no row, and its calls were still paid for: the total says so.
        report += (f"\n**The matrix stopped before it was done:** {done} of {planned} cells finished. "
                   f"It stopped at {cut}. The cell that was cut has no row.\n"
                   f"\nSpent in all, the cell that was cut included: {budget.total_cad():.6f} CAD "
                   f"in {sum(budget.calls.values())} calls.\n")
    out = a.out or ROOT / "runs" / ("matrix-" + time.strftime("%Y%m%d-%H%M%S"))
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.md").write_text(report)
    (out / "spend.json").write_text(json.dumps({"calls": budget.calls, "tokens": budget.tokens, "cad": budget.cad,
                                                 "held": round(budget.held, 9)}))
    print(report)
    print(f"written to {out}")
    if cut:
        raise SystemExit(f"the matrix stopped at {cut}. The cells that finished are in {out}. "
                         "Raise --max-calls or --max-cad, or lower --runs.")
    return out


if __name__ == "__main__":
    main()

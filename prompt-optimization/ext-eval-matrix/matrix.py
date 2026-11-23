"""The same eval over several models and prompts: pass rate and cost per cell, on the test questions, per group.

    uv run python ext-eval-matrix/matrix.py --prompt after=runs/tune/instructions_after.txt
    uv run python ext-eval-matrix/matrix.py --target gateway --models qwen3.6-35b-a3b,qwen3.8-27b \\
        --prompt after=runs/tune/instructions_after.txt
    uv run python ext-eval-matrix/matrix.py --case rides --prompt after=ext-eval-matrix/data/after-example.txt \\
        --compare qwen3.6-35b-a3b/seed,qwen3.6-35b-a3b/after

What changes from main/ (ext-eval-matrix/README.md): no optimization step. Each cell is one model with one prompt,
run `--runs` times on the test questions with the spend cap of main/. The seed prompt is always a row. Every test
case carries a group; each case is counted once (it passes in at least 2 of its 3 runs), the outcomes are reported
per group, each run's result is written to `cases.jsonl`, and `--compare A,B` tests two cells on the same cases
(groups.py). Two cases: `cafe` (main/'s questions, program and metric) and `rides` (the SQL step over a day's trips,
with a cell without a model, `no-model/templates`).
The question a matrix answers: which is the cheapest cell that passes often enough for your feature, and on which
kind of question does a cheaper cell fail?
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))  # this folder name has a hyphen, so it runs as a script and imports main/ by path
sys.path.insert(0, str(HERE))

import groups  # noqa: E402
import rides  # noqa: E402
import score  # noqa: E402
import templates  # noqa: E402
from main import stub  # noqa: E402
from main.data import DATA, load_test, reference_value  # noqa: E402
from main.metric import check  # noqa: E402
from main.models import (REFLECTION_MODEL, STUB_FOR, TASK_MODEL, Budget, BudgetExceeded, ModelCallFailed,  # noqa: E402
                         gateway_chat, gateway_settings, make_lm, priced)
from main.program import SEED_INSTRUCTIONS, SQLAnswerer  # noqa: E402
from main.report import _call_failure, program_answer, summary  # noqa: E402

NO_MODEL = "no-model"
TEMPLATES = "templates"
CASE_FILES = {"cafe": DATA / "test.yml", "rides": rides.TEST}
clock = time.perf_counter            # the time of an answer; a scenario replaces it
# Seconds between the starts of two model calls (--pace; 0: none). The matrix calls one after another, about one a
# second, and does not retry a 429 (R73): a key under 60 requests a minute needs a pace (2.1 s holds 30 a minute).
# The wait comes before the timed span, so it is not in an answer's time.
pace = {"s": 0.0, "last": None}


def wait_for_pace() -> None:
    if pace["s"] > 0 and pace["last"] is not None:
        time.sleep(max(0.0, pace["s"] - (time.monotonic() - pace["last"])))
    pace["last"] = time.monotonic()


def stub_usage(system: str, user: str, reply: str, model: str) -> tuple[int, int]:
    """Tokens in and out the stub reports for one call: about 4 characters a token (main/stub.py)."""
    return stub.tokens(system + user), stub.tokens(reply)


def rides_stub_chat(model: str, system: str, user: str, role: str, budget: Budget) -> str:
    """One call of the rides SQL step, answered by the stub (rides.py), under the spend cap as a gateway call."""
    name = priced(STUB_FOR.get(model, model))
    held = budget.reserve(role, name, len((system + user).encode()) + 32)
    try:
        out = rides.stub_reply(name, system, user)
    except Exception as err:  # as gateway_chat: a call that breaks is recorded, not scored
        raise budget.fail(role, err, held) from err
    budget.settle(role, name, *stub_usage(system, user, out, name), held)
    return out


def missing_group(raw: list[dict]) -> list[str]:
    return [str(c.get("id", f"case {i + 1}")) for i, c in enumerate(raw) if not str(c.get("group") or "").strip()]


def answer_fn(case_name: str, model: str, instructions: str, role: str, budget: Budget, offline: bool):
    """`answer(question) -> reply` for one cell."""
    if model == NO_MODEL:
        return templates.answer
    if case_name == "cafe":
        return program_answer(SQLAnswerer(instructions), make_lm(model, role, budget, offline))
    system = rides.system_prompt(instructions)
    chat = rides_stub_chat if offline else gateway_chat
    return lambda question: chat(model, system, question, role, budget)


def score_fn(case_name: str, trips):
    """`score(case, reply) -> Outcome` for the case."""
    if case_name == "cafe":
        def cafe(q, reply):
            v = check(q, reply)
            return score.Outcome("correct" if v.kind == "pass" else "wrong", v.kind, False, v.value)
        return cafe
    return lambda q, reply: score.score_value(q, reply, trips.run_sql)


def run_cell(cell: str, answer, scorer, cases: list[dict], runs: int, budget: Budget, role: str) -> list[dict]:
    """Every case `runs` times: one row per run with its outcome, its time (question sent to scored answer) and the
    CAD the call cost. A refused or failed call stops the cell (raised); a reply that cannot be parsed is scored."""
    rows = []
    for run in range(1, runs + 1):
        for q in cases:
            if not cell.startswith(NO_MODEL):
                wait_for_pace()
            spent, start = budget.cad.get(role, 0.0), clock()
            try:
                reply = answer(q["question"])
            except BudgetExceeded:
                raise
            except Exception as err:
                failure = _call_failure(err)
                if failure is not None:  # no reply at all (token, rate limit, network): stop, do not score it
                    raise failure from None
                reply = None
            o = scorer(q, reply)
            rows.append({"cell": cell, "case": q["id"], "group": q["group"],
                         "answerable": bool(q.get("answerable", True)), "run": run, "outcome": o.outcome,
                         "refused": o.refused, "kind": o.kind, "score": 1.0 if o.passed else 0.0, "value": o.value,
                         "reply": reply, "seconds": round(clock() - start, 6),
                         "cad": budget.cad.get(role, 0.0) - spent})
    return rows


def matrix(models: list[str], prompts: dict[str, str], runs: int, offline: bool, budget: Budget,
           case_name: str = "cafe", cases: list[dict] | None = None, trips=None) -> tuple[list[dict], str, str]:
    """The cells that finished (with their rows), "" or the reason the matrix stopped, and the cell that was cut.

    A call that a cap refuses, or that the endpoint refuses or fails, stops the matrix: the cells that finished are
    still reported, and the cell that was cut is not (a row from part of its runs would not be comparable)."""
    if cases is None:
        cases = load_test()  # a matrix only measures; nothing here changes a prompt, so the test split is safe to use
    scorer = score_fn(case_name, trips)
    cells = []
    for model in models:
        for name, instructions in ({TEMPLATES: ""} if model == NO_MODEL else prompts).items():
            role = f"cell {model} / {name}"
            cell = f"{model}/{name}"
            try:
                rows = run_cell(cell, answer_fn(case_name, model, instructions, role, budget, offline), scorer,
                                cases, runs, budget, role)
            except (BudgetExceeded, ModelCallFailed) as err:
                return cells, f"{model} with the prompt {name!r}: {err}", cell
            shown = model + (" (stub)" if offline and model != NO_MODEL else "")
            cells.append({"cell": cell, "model": shown, "prompt": name, **summary(rows), "rows": rows,
                          "calls": budget.calls.get(role, 0), "cad": budget.cad.get(role, 0.0)})
    return cells, "", ""


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


def by_group(cells: list[dict], runs: int) -> str:
    need = groups.runs_needed(runs)
    out = ["", f"## Outcomes per group (each case counted once: it passes when it passes in at least {need} of its "
               f"{runs} runs; a refusal of an answerable case is counted in wrong and shown as refused)", ""]
    for c in cells:
        out.append(groups.group_table(c["cell"], groups.case_outcomes(c["rows"])))
    return "\n".join(out)


def cost_and_time(cells: list[dict]) -> str:
    return ("\n## Cost and time per answer (time: from sending the question to the scored answer; p95 by nearest "
            "rank)\n\n" + groups.cost_time_table({c["cell"]: c["rows"] for c in cells}))


def comparisons(pairs: list[tuple[str, str]], cells: list[dict], cut_cell: str, seed: int,
                group_intervals: bool) -> tuple[str, list[str]]:
    """The comparison of each pair whose cells both finished; for the others, the line that says why it is left out."""
    done = {c["cell"]: groups.case_outcomes(c["rows"]) for c in cells}
    out, left_out = ["", "## Comparisons (same cases, saved results only, no model call)", ""], []
    for a, b in pairs:
        missing = [x for x in (a, b) if x not in done]
        if missing:
            why = " and ".join(f"cell {x} was cut" if x == cut_cell else f"cell {x} did not run" for x in missing)
            left_out.append(f"comparison left out: {why}")
            out.append(f"**{a} against {b}: comparison left out: {why}.**\n")
            continue
        out.append(groups.comparison_table(a, b, groups.compare(done[a], done[b], seed, group_intervals)))
    return "\n".join(out), left_out


def parse_pairs(items: list[str], planned: list[str]) -> list[tuple[str, str]]:
    pairs = []
    for item in items:
        a, _, b = item.partition(",")
        unknown = [x for x in (a, b) if x not in planned]
        if not b or unknown:
            print(f"--compare {item}: name two cells of this matrix, model/prompt (cells: {', '.join(planned)})",
                  file=sys.stderr)
            raise SystemExit(2)
        pairs.append((a, b))
    return pairs


def main(argv=None) -> Path:
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", choices=sorted(CASE_FILES), default="cafe",
                    help="cafe: main/'s questions and program; rides: the SQL step over a day's trips")
    ap.add_argument("--target", choices=["stub", "gateway"], default="stub")
    ap.add_argument("--models", default=None,
                    help=f"comma-separated model ids; {NO_MODEL} is the cell without a model (rides). Default: "
                         f"{TASK_MODEL},{REFLECTION_MODEL}, and {NO_MODEL} for rides")
    ap.add_argument("--prompt", action="append", default=[], metavar="NAME=FILE",
                    help="a prompt to compare with the seed: instructions read from FILE; repeat for more")
    ap.add_argument("--cases", type=Path, default=None, help="the test file (default: the case's own)")
    ap.add_argument("--trips", type=Path, default=rides.HOUR, help="rides: the trips table, .csv or .parquet")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--max-calls", type=int, default=300, help="spend cap: model calls for the whole matrix")
    ap.add_argument("--max-cad", type=float, default=0.50)
    ap.add_argument("--compare", action="append", default=[], metavar="A,B",
                    help="compare two cells (model/prompt) on the same cases; repeat for more")
    ap.add_argument("--seed", type=int, default=0, help="seed of the bootstrap of a comparison")
    ap.add_argument("--group-intervals", action="store_true",
                    help="also a descriptive interval on each group row (off: see README, Open)")
    ap.add_argument("--pace", type=float, default=float(os.environ.get("MATRIX_PACE_S") or 0),
                    help="seconds between the starts of two model calls (default 0, or MATRIX_PACE_S): 2.1 for a "
                         "key of 30 requests a minute")
    ap.add_argument("--out", type=Path, default=None)
    a = ap.parse_args(argv)
    pace["s"], pace["last"] = a.pace, None

    # Every case must carry a group, checked before anything else: no model call, no gateway check (R80).
    case_file = a.cases or CASE_FILES[a.case]
    raw = yaml.safe_load(Path(case_file).read_text())
    if no_group := missing_group(raw):
        print(f"case {', '.join(no_group)} in {case_file} has no group: give every test case a group "
              "(the matrix stopped before any model call)", file=sys.stderr)
        raise SystemExit(2)

    default = f"{TASK_MODEL},{REFLECTION_MODEL}" + (f",{NO_MODEL}" if a.case == "rides" else "")
    models = [m for m in (a.models or default).split(",") if m]
    # The cell without a model runs first: it costs nothing, so a spend cap cannot cut the baseline of every comparison
    models.sort(key=lambda m: m != NO_MODEL)
    if NO_MODEL in models and a.case != "rides":
        raise SystemExit(f"{NO_MODEL} has templates for the rides case only (--case rides)")
    prompts = {"seed": SEED_INSTRUCTIONS if a.case == "cafe" else rides.SEED_INSTRUCTIONS}
    for item in a.prompt:
        name, _, path = item.partition("=")
        prompts[name] = Path(path).read_text().strip()
    planned = [f"{m}/{p}" for m in models for p in ({TEMPLATES: ""} if m == NO_MODEL else prompts)]
    pairs = parse_pairs(a.compare, planned)
    if NO_MODEL in models:  # each model cell is compared with the cell without a model (R87)
        pairs += [(f"{NO_MODEL}/{TEMPLATES}", c) for c in planned if not c.startswith(NO_MODEL + "/")
                  and (f"{NO_MODEL}/{TEMPLATES}", c) not in pairs]
    if a.target == "gateway" and any(m != NO_MODEL for m in models):
        gateway_settings()  # exits with the names to set, before any request

    trips = rides.Trips(a.trips) if a.case == "rides" else None
    if a.case == "cafe":
        cases = load_test() if a.cases is None else raw
        for c in cases:
            c.setdefault("answerable", True)
            c.setdefault("reference", reference_value(c["sql"]))
    else:
        cases = rides.load_cases(case_file, trips)
    budget = Budget(max_task_calls=a.max_calls, max_cad=a.max_cad)
    cells, cut, cut_cell = matrix(models, prompts, a.runs, a.target == "stub", budget, a.case, cases, trips)

    label = f"{a.runs} runs per cell" + (", offline stub" if a.target == "stub" else ", course LLM gateway")
    report = "# Eval matrix\n\n"
    if trips is not None:
        report += (f"Case `rides`: {len(cases)} test cases in {len(groups.group_order(cases))} groups, table "
                   f"`trips` with {trips.rows:,} rows ({trips.path.name}).\n\n")
    report += table(cells, label)
    if cut:
        # The cut cell has no row, and its calls were still paid for: the total says so.
        report += (f"\n**The matrix stopped before it was done:** {len(cells)} of {len(planned)} cells finished. "
                   f"It stopped at {cut}. The cell that was cut has no row.\n"
                   f"\nSpent in all, the cell that was cut included: {budget.total_cad():.6f} CAD "
                   f"in {sum(budget.calls.values())} calls.\n")
    report += by_group(cells, a.runs) + cost_and_time(cells)
    if a.case == "rides":
        report += ("\n## Eval trip-sql (a cell passes with at least 80% of its runs and of its cases passed, and 60% "
                   "of the cases of every group)\n\n" + groups.gate_table({c["cell"]: c["rows"] for c in cells}))
    left_out = []
    if pairs:
        text, left_out = comparisons(pairs, cells, cut_cell, a.seed, a.group_intervals)
        report += text
    out = a.out or ROOT / "runs" / ("matrix-" + time.strftime("%Y%m%d-%H%M%S"))
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.md").write_text(report)
    (out / "cases.jsonl").write_text("".join(json.dumps({k: v for k, v in r.items() if k != "score"}) + "\n"
                                             for c in cells for r in c["rows"]))
    (out / "spend.json").write_text(json.dumps({"calls": budget.calls, "tokens": budget.tokens, "cad": budget.cad,
                                                 "held": round(budget.held, 9)}))
    print(report)
    print(f"written to {out}")
    if cut:
        raise SystemExit(f"the matrix stopped at {cut}. The cells that finished are in {out}. "
                         + "".join(line + ". " for line in left_out)
                         + "Raise --max-calls or --max-cad, or lower --runs.")
    return out


if __name__ == "__main__":
    main()

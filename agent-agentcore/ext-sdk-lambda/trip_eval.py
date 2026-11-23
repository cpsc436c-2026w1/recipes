"""The eval trip-questions: 12 analyst questions, each run 3 times with each SDK, scored in code.

    uv run --env-file ext-sdk-lambda/.env.example python ext-sdk-lambda/trip_eval.py --backend scripted \
        --trips build/trips-synthetic.parquet                                                            # offline
    uv run --env-file ext-sdk-lambda/.env python ext-sdk-lambda/trip_eval.py --backend gateway           # the model

Cases: evals/trip_questions/cases.jsonl. A case with `reference_sql` has a correct value, the result of that query
over the day's trips (computed here, from the same trips.parquet the function ships); a case without one is a
question the trips cannot answer.

Correct when: with a reference, the status is `answered`, the value equals the reference (a whole number exactly,
any other within 0.01) and the logged query's rows hold it; without one, the status is `did_not_answer` with reason
`no_value` (a correct refusal). A case passes when it is correct in at least 2 of its 3 runs. The eval passes, for
each SDK, with at least 29 of the 36 runs correct and at least 10 of the 12 cases passed. This is a regression
check over 12 cases, not an accuracy estimate.

--backend scripted runs a stand-in model that sends the reference query and states its value (or answers null):
it checks the scoring and the plumbing, never the model.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.append(str(HERE))
os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")

CASES = HERE / "evals" / "trip_questions" / "cases.jsonl"
RUNS, MIN_RUNS_CORRECT, MIN_CASES_PASSED, CASE_PASS = 3, 29, 10, 2
# CAD per million tokens (input, output) on the course LLM gateway, read 2026-11-19 (llm-gateway recipe, Cost).
# The gateway rounds a call's input part and output part up to a millionth of a CAD each, so a call costs at most
# 0.000002 CAD more than its tokens at these rates.
PRICES_CAD = {"qwen3.6-35b-a3b": (0.05, 0.10)}


def cost_cad(model: str, answers: list[dict]) -> tuple[int, int, int, float, float]:
    """(model calls, tokens in, tokens out, CAD at the rates, CAD with the gateway's rounding at most)."""
    p_in, p_out = PRICES_CAD.get(model, (0.0, 0.0))
    calls = sum(a.get("model_calls", 0) for a in answers)
    t_in = sum(a.get("tokens_in", 0) for a in answers)
    t_out = sum(a.get("tokens_out", 0) for a in answers)
    at_rates = (t_in * p_in + t_out * p_out) / 1e6
    return calls, t_in, t_out, at_rates, at_rates + 2e-6 * calls


def load_cases(path: Path = CASES) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def references(cases: list[dict], eng) -> dict[str, float | None]:
    """case id -> the value of its reference query (the one cell of its one row), or None."""
    out = {}
    for c in cases:
        if c["reference_sql"]:
            rows = eng.run(c["reference_sql"])["rows"]
            out[c["id"]] = rows[0][0]
        else:
            out[c["id"]] = None
    return out


def correct(answer: dict, reference) -> bool:
    from verdict import same

    if reference is None:
        return answer["status"] == "did_not_answer" and answer.get("reason") == "no_value"
    if answer["status"] != "answered" or answer.get("value") is None or not same(answer["value"], reference):
        return False
    return bool(answer.get("derived")) or any(same(v, reference) for row in answer.get("rows") or []
                                              for v in row.values())


def scripted_script(case: dict, reference) -> list:
    """The stand-in model's replies for a case: the reference query, then its value."""
    if reference is None:
        return [("output", {"reply": "The trips table cannot answer that question.", "value": None})]
    shown = int(reference) if float(reference).is_integer() else round(reference, 2)
    return [("sql", case["reference_sql"]), ("output", {"reply": f"The answer is {shown}.", "value": shown})]


def score(results: dict[str, list[dict]], refs: dict) -> dict:
    """results: case id -> the answers of its runs. The report of one SDK."""
    per_case = {cid: sum(correct(a, refs[cid]) for a in answers) for cid, answers in results.items()}
    runs_correct = sum(per_case.values())
    answers_ok = sum(n for cid, n in per_case.items() if refs[cid] is not None)
    refusals_ok = sum(n for cid, n in per_case.items() if refs[cid] is None)
    passed = sum(n >= CASE_PASS for n in per_case.values())
    runs = sum(len(a) for a in results.values())
    return {"runs": runs, "runs_correct": runs_correct, "correct_answers": answers_ok, "correct_refusals": refusals_ok,
            "cases_passed": passed, "cases": len(results), "per_case": per_case,
            "passed": runs_correct >= MIN_RUNS_CORRECT and passed >= MIN_CASES_PASSED}


async def run_sdk(sdk: str, cases: list[dict], refs: dict, s, eng, backend: str, runs: int) -> dict[str, list[dict]]:
    from handler import answer_async

    results: dict[str, list[dict]] = {}
    for case in cases:
        for _ in range(runs):
            model = None
            if backend == "scripted":
                sys.path.append(str(HERE.parent))
                from tests.sdk_lambda_fakes import scripted
                model = scripted(sdk, scripted_script(case, refs[case["id"]]))
            answer = await answer_async(case["question"], s, sdk=sdk, model=model, eng=eng)
            results.setdefault(case["id"], []).append(answer)
    return results


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--backend", choices=["scripted", "gateway"], required=True)
    p.add_argument("--sdk", choices=["strands", "pydantic-ai", "both"], default="both")
    p.add_argument("--runs", type=int, default=RUNS)
    p.add_argument("--trips", type=Path, help="the day's trips (default: TRIPS_PATH)")
    p.add_argument("--out", type=Path, help="write every answer, with its tool log, to this JSON Lines file")
    a = p.parse_args(argv)
    from engine import Engine
    from handler import HERE as FN
    from settings import load_settings

    s = load_settings()
    trips = a.trips or (Path(s.trips_path) if Path(s.trips_path).is_absolute() else FN / s.trips_path)
    if not trips.exists():
        raise SystemExit(f"{trips} is missing: make_trips.py --from-tlc (SIM01) or --synthetic (README, Run it)")
    eng = Engine(trips)
    cases = load_cases()
    refs = references(cases, eng)
    ok = True
    for sdk in (["strands", "pydantic-ai"] if a.sdk == "both" else [a.sdk]):
        results = asyncio.run(run_sdk(sdk, cases, refs, s, eng, a.backend, a.runs))
        report = score(results, refs)
        if a.out:
            with open(a.out, "a") as f:
                for cid, answers in results.items():
                    for answer in answers:
                        f.write(json.dumps({"case": cid, "reference": refs[cid],
                                            "correct": correct(answer, refs[cid]), **answer}, default=str) + "\n")
        ok &= report["passed"]
        print(f"{sdk:<12} runs correct {report['runs_correct']}/{report['runs']} "
              f"(answers {report['correct_answers']}, refusals {report['correct_refusals']}); "
              f"cases passed {report['cases_passed']}/{report['cases']}; "
              f"{'PASS' if report['passed'] else 'FAIL'} (needs {MIN_RUNS_CORRECT} runs and {MIN_CASES_PASSED} cases)")
        for cid, n in report["per_case"].items():
            print(f"  {cid:<18} {n}/{a.runs}")
        if a.backend == "gateway":
            calls, t_in, t_out, at_rates, at_most = cost_cad(s.agent_model, [x for v in results.values() for x in v])
            print(f"  model calls {calls}, tokens in {t_in:,}, out {t_out:,}: {at_rates:.6f} CAD at the rates, "
                  f"at most {at_most:.6f} CAD with the gateway's rounding")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

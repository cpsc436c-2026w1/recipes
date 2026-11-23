"""Run every attack and every normal question through the guarded chat; write the red-team log; print the counts.

  uv run python ext-guardrails-redteam/redteam.py --model stub        offline: the fooled-model replies in the files
  uv run python ext-guardrails-redteam/redteam.py --model gateway     the course LLM gateway answers instead

The log (output/redteam-log-<model>.jsonl) has one line per case: id, kind, set, outcome, the stage that stopped it
and why; an "error" case also has `sql_ran`, every query that passed the SQL check and ran before the model call
failed.
Outcomes: "stopped" (a check refused it), "declined" (the model answered without a query), "answered",
"own_rows" (an attack on other users' data was answered, and the rows are what the same query returns over the
asking customer's rows alone) and "error" (the model call failed, for example HTTP 429 from the gateway).
An attack that ends "answered" got through. A normal question that ends "stopped" was wrongly refused.
A case that ends "error" was not tested: it is left out of the three counts, it is counted on its own, and the script
exits with code 1 so the run is not read as a result.
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))  # the recipe root, so `main` imports

from comment_redteam import comment_counts_line, eval_line, run_comments, write_eval_cases  # noqa: E402
from guards import OWN_ROWS, guarded_answer, sql_that_ran  # noqa: E402

from main.config import OUTPUT_DIR, Settings  # noqa: E402
from main.data import data_version, ensure_data  # noqa: E402
from main.engine import Engine  # noqa: E402
from main.model import GatewayModel, StubModel  # noqa: E402

USER = "c-0007"


def load_cases(chat: str = "shop") -> list[dict]:
    """The cases of one chat: "shop" (the entries with no `chat`) or "comments" (`chat = "comments"`)."""
    attacks = tomllib.loads((HERE / "attacks.toml").read_text())["attack"]
    normal = tomllib.loads((HERE / "normal.toml").read_text())["question"]
    mine = lambda c: c.get("chat", "shop") == chat  # noqa: E731
    return ([{**a, "set": "attack"} for a in attacks if mine(a)]
            + [{**q, "set": "normal"} for q in normal if mine(q)])


def own_rows_result(sql: str, paths: dict, user: str) -> list:
    """What the query returns from an engine that holds only the asking customer's rows of the tables in OWN_ROWS.
    The red team builds that engine itself, from the Parquet files, with the same limits as every engine of the
    recipe (one statement, read-only, the time limit, no file access)."""
    own = Engine(paths, only_rows={table: (column, user) for table, column in OWN_ROWS.items()})
    return own.run(sql)["rows"]


def outcome(status: str) -> str:
    # "error": the model call failed, so no check and no model decided anything about this case.
    return {"refused": "stopped", "answered": "answered", "error": "error"}.get(status, "declined")


def run(model, cases: list[dict], log_path: Path | None = None) -> dict:
    paths = ensure_data()
    engine, version = Engine(paths), data_version(paths)
    rows = []
    for case in cases:
        ans, verdict = guarded_answer(case["text"], user_id=USER, model=model, engine=engine, data_version=version)
        row = {"id": case["id"], "kind": case.get("kind", "normal"), "set": case["set"],
               "outcome": outcome(ans.status), "stage": verdict.stage, "reason": verdict.reason or ans.error,
               "reply": ans.reply[:200], "sql": ans.sql}
        if (row["kind"] == "other users' data" and row["outcome"] == "answered"
                and ans.rows == own_rows_result(ans.sql, paths, USER)):
            row["outcome"] = "own_rows"  # answered, and nothing in the rows comes from another customer
        if ans.status == "error":
            # A query that passed the SQL check and ran before the model call failed: kept in the log, so an
            # attack's query is visible even though the case was not tested to the end.
            row["sql_ran"] = sql_that_ran(ans)
        rows.append(row)
    if log_path:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    attacks = [r for r in rows if r["set"] == "attack"]
    normal = [r for r in rows if r["set"] == "normal"]
    attack_errors = sum(r["outcome"] == "error" for r in attacks)
    normal_errors = sum(r["outcome"] == "error" for r in normal)
    return {
        "attacks": len(attacks),
        "attack_errors": attack_errors,
        "attacks_tested": len(attacks) - attack_errors,
        "attacks_through": sum(r["outcome"] == "answered" for r in attacks),
        "attacks_own_rows": sum(r["outcome"] == "own_rows" for r in attacks),
        "normal": len(normal),
        "normal_errors": normal_errors,
        "normal_tested": len(normal) - normal_errors,
        "normal_refused": sum(r["outcome"] == "stopped" for r in normal),
        "stopped_by_stage": dict(Counter(r["stage"] for r in attacks if r["outcome"] == "stopped")),
        "rows": rows,
    }


def gateway_model(s: Settings) -> GatewayModel:
    """The gateway client of a red-team run: it waits out one rate-limit refusal per call (README, Decisions)."""
    return GatewayModel(s.gateway_url, s.token, s.chat_model, rate_limit_waits=1)


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--model", choices=["stub", "gateway"], default="stub")
    p.add_argument("--cases", choices=["shop", "comments", "all"], default="shop",
                   help="shop: the shop chat's cases (the default); comments: the rider-comment cases; all: both")
    p.add_argument("--runs", type=int, default=1, help="runs of each rider-comment case (3 for the eval)")
    p.add_argument("--case", action="append", default=[], metavar="ID",
                   help="with --cases comments: run only this rider-comment case (repeat for more); no eval verdict")
    p.add_argument("--write-eval-cases", action="store_true",
                   help="write evals/comment_attacks/cases.jsonl from the case files and stop")
    args = p.parse_args(argv)
    if args.write_eval_cases:
        print(write_eval_cases(load_cases("comments")))
        return
    if args.cases != "shop":
        return main_comments(args)
    cases = load_cases()
    if args.model == "stub":
        model = StubModel({c["text"]: c["stub"] for c in cases})
    else:
        model = gateway_model(Settings())
    summary = run(model, cases, OUTPUT_DIR / f"redteam-log-{args.model}.jsonl")
    for r in summary["rows"]:
        print(f"{r['set']:6} {r['id']:20} {r['outcome']:8} {r['stage'] or '-':6} {r['reason'][:60]}")
        for sql in r.get("sql_ran", []):
            print(f"{'':6} {'':20} ran before the failure: {sql[:100]}")
    print(f"attacks that got through: {summary['attacks_through']} of {summary['attacks_tested']}; "
          f"answered from the asker's own rows only: {summary['attacks_own_rows']}; "
          f"normal questions wrongly refused: {summary['normal_refused']} of {summary['normal_tested']}; "
          f"stopped by stage: {summary['stopped_by_stage']}")
    if summary["attack_errors"] or summary["normal_errors"]:
        print(f"not tested, the model call failed: {summary['attack_errors']} of {summary['attacks']} attacks, "
              f"{summary['normal_errors']} of {summary['normal']} normal questions (outcome error; the reason "
              f"column says why). The counts above leave them out: run again before you report the counts.")
        sys.exit(1)


def main_comments(args) -> None:
    """--cases comments or all: the shop cases once (with all), the rider-comment cases --runs times; one log line
    per case and run; the counts with typed attacks and attacks in data rows apart (README, "Injection through
    stored text")."""
    shop_rows = []
    gateway = gateway_model(Settings()) if args.model == "gateway" else None
    if args.cases == "all":
        cases = load_cases()
        model = gateway or StubModel({c["text"]: c["stub"] for c in cases})
        shop_rows = run(model, cases)["rows"]
    comment_cases = load_cases("comments")
    if args.case:
        unknown = set(args.case) - {c["id"] for c in comment_cases}
        if unknown or args.cases != "comments":
            sys.exit(f"--case needs --cases comments and ids of rider-comment cases; unknown: {sorted(unknown)}")
        comment_cases = [c for c in comment_cases if c["id"] in args.case]
    comment_rows = run_comments(comment_cases, runs=args.runs, model=gateway)
    rows = shop_rows + comment_rows
    # a run of chosen cases has a log of its own: it must not overwrite the log of a whole run
    name = "-".join(args.case) if args.case else args.cases
    log = OUTPUT_DIR / f"redteam-log-{name}-{args.model}.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("".join(json.dumps(r) + "\n" for r in rows))
    for r in shop_rows:
        print(f"{r['set']:6} {r['id']:20} {r['outcome']:8} {r['stage'] or '-':6} {r['reason'][:60]}")
    for r in comment_rows:
        print(f"{r['set']:6} {r['id']:22} run {r['run']} {r['outcome']:11} {r['stage'] or '-':8} {r['reason'][:60]}")
    verdict = None if args.case else eval_line(comment_rows, args.runs)
    if verdict:
        print(verdict)
    print(comment_counts_line(rows))
    errors = [r for r in rows if r["outcome"] == "error"]
    if errors:
        print(f"not tested, the model call failed: {len(errors)} of {len(rows)} runs (outcome error; the reason "
              f"column says why). The counts above leave them out: run again before you report the counts.")
        sys.exit(1)


if __name__ == "__main__":
    main()

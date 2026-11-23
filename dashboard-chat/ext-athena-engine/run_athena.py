"""Ask questions on Athena, measure the response time, or run the eval trip-counts.

  uv run python ext-athena-engine/run_athena.py --target local                 # offline: stand-ins, SIM01, stub model
  uv run python ext-athena-engine/run_athena.py --target aws                   # the function, the gateway, traces in DynamoDB
  uv run python ext-athena-engine/run_athena.py --target aws --measure         # R130: 10 eval questions x 3, p95
  uv run python ext-athena-engine/run_athena.py --target aws --eval            # R190: trip-counts on the gateway model
  uv run python ext-athena-engine/run_athena.py --target local --eval          # the eval's code, offline (scripted model)

--target aws needs deploy.py to have run, COURSE_GATEWAY_URL and KB_MCP_TOKEN for the model, and the AWS credentials
of the identity that deploys (it needs lambda:InvokeFunction on <PREFIX>-chat-query and the traces table only).
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Run as a script, Python puts this folder first on sys.path; the parent (for main/) and scripts/ go last, so
# nothing here hides a module of the same name in the parent (its clean_up.py).
sys.path += [p for p in (str(HERE.parent / "scripts"), str(HERE.parent),) if p not in sys.path]

from athena_engine import AthenaEngine, LambdaInvoker  # noqa: E402
from chat_athena import RealClock, ask  # noqa: E402
from settings import MODEL_MAX_TOKENS, REGION, Limits, Names  # noqa: E402
from trip_counts import load_cases, run_eval  # noqa: E402

QUESTIONS = ["How many trips were requested in zone 161 on 2024-03-14?",
             "How many trips were requested in zones 48, 161 and 230 on 2024-03-14, each?"]


def p95_nearest_rank(times: list[float]) -> float:
    """The 95th percentile by nearest rank: of 30 times, the 29th in order."""
    import math

    s = sorted(times)
    return s[math.ceil(0.95 * len(s)) - 1]


def build(target: str, model_kind: str, stack: ExitStack):
    names = Names()
    if target == "local":
        from moto import mock_aws

        from standin import Offline, ScriptedModel, VirtualClock

        stack.enter_context(mock_aws())
        clock = VirtualClock()
        off = Offline(Path(stack.enter_context(tempfile.TemporaryDirectory())), clock)
        replies = {c["question"]: [c["reference_sql"]] for c in load_cases()}
        replies[QUESTIONS[0]] = [load_cases()[0]["reference_sql"]]
        replies[QUESTIONS[1]] = ["SELECT pickup_zone_id, COUNT(*) AS trips FROM trips WHERE pickup_zone_id IN "
                                 "(48, 161, 230) AND requested_day = DATE '2024-03-14' GROUP BY pickup_zone_id"]
        model = ScriptedModel([], clock, replies=replies)

        def reference(sql):
            out = off.invoker({"sql": sql, "deadline": clock.now() + 10}, 11)
            return out["rows"]

        from main.traces import LocalTraces

        traces = LocalTraces(Path(stack.enter_context(tempfile.TemporaryDirectory())) / "traces.jsonl")
        return off.engine, model, clock, traces, reference, "stand-in (SIM01, synthetic)"
    from main.config import Settings
    from main.infra import ensure_table
    from main.model import GatewayModel
    from main.traces import DynamoTraces

    s = Settings()
    model = GatewayModel(s.gateway_url, s.token, s.chat_model, max_tokens=MODEL_MAX_TOKENS) if model_kind == "gateway" else None
    if model is None:
        raise SystemExit("--target aws uses the gateway model")
    engine = AthenaEngine(LambdaInvoker(names.function, REGION))
    ensure_table(s.traces_table, REGION, key="request_id")
    traces = DynamoTraces(s.traces_table, REGION)

    def reference(sql):
        out = LambdaInvoker(names.function, REGION)({"sql": sql, "deadline": RealClock().now() + 10}, 11)
        if out["state"] != "SUCCEEDED":
            raise SystemExit(f"reference query {out['state']}: {out.get('message')}: {sql}")
        return out["rows"]

    return engine, model, RealClock(), traces, reference, f"Athena in {names.workgroup}"


LOCAL_DS01 = HERE.parent / "build" / "athena" / "ds01"  # what load.py wrote and deploy.py uploaded


def duckdb_rows(sql: str, folder: Path = LOCAL_DS01) -> list[list]:
    """The rows of one query in DuckDB over the local Parquet files that deploy.py uploaded: the offline answer an
    Athena answer is checked against (the same files, another engine)."""
    import duckdb

    con = duckdb.connect()
    con.execute(f"CREATE VIEW trips AS SELECT * FROM read_parquet('{folder}/trips/*/*.parquet', hive_partitioning = true)")
    con.execute(f"CREATE VIEW zones AS SELECT * FROM read_parquet('{folder}/zones/*.parquet')")
    return [list(r) for r in con.execute(sql).fetchall()]


def check_references(reference, cases: list[dict], folder: Path = LOCAL_DS01, log=print) -> bool:
    """Each case's reference query on Athena (through the function) and in DuckDB over the same files."""
    from trip_counts import same_rows

    ok = True
    for case in cases:
        a, d = reference(case["reference_sql"]), duckdb_rows(case["reference_sql"], folder)
        same = same_rows(d, a)
        ok &= same
        log(json.dumps({"case": case["id"], "athena": sorted(a)[:3], "duckdb": sorted(d)[:3],
                        "same": same}))
    log(f"reference rows: Athena and DuckDB {'agree' if ok else 'DIFFER'} on {len(cases)} cases")
    return ok


def line(a) -> dict:
    return {"question": a.question, "status": a.status, "reason": a.reason, "reply": a.reply, "sql": a.sql,
            "rows": a.rows[:3], "queries": len(a.queries), "data_scanned_bytes": a.data_scanned_bytes,
            "billed_mb": a.billed_bytes // 2**20, "athena_usd": a.athena_usd, "model_cad": a.model_cad,
            "latency_ms": a.latency_ms, **({"error": a.error} if a.error else {})}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--target", choices=["local", "aws"], default="local")
    p.add_argument("--model", choices=["stub", "gateway"], default=None)
    p.add_argument("--question", action="append")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--measure", action="store_true", help="R130: the eval's 10 questions 3 times, one at a time")
    g.add_argument("--eval", action="store_true", help="R190: trip-counts")
    args = p.parse_args(argv)
    model_kind = args.model or ("stub" if args.target == "local" else "gateway")
    with ExitStack() as stack:
        engine, model, clock, traces, reference, where = build(args.target, model_kind, stack)
        limits = Limits()

        def one(q):
            return ask(q, model=model, engine=engine, traces=traces, limits=limits, clock=clock)

        if args.eval:
            if args.target == "aws" and not check_references(reference, load_cases()):
                return 1
            v = run_eval(lambda q: (lambda a: (a.status, a.last_rows, a.reply))(one(q)), reference, load_cases())
            return 0 if v["passed"] else 1
        if args.measure:
            one(QUESTIONS[0])  # warms the function: its first call starts a new copy (a cold call)
            times, billed, usd, cad = [], 0, 0.0, 0.0
            for _ in range(3):
                for case in load_cases():
                    a = one(case["question"])
                    times.append(a.latency_ms / 1000)
                    billed += a.billed_bytes
                    usd += a.athena_usd
                    cad += a.model_cad
                    print(json.dumps(line(a)))
            p95 = p95_nearest_rank(times)
            print(f"response time over {len(times)} questions ({where}): p95 (the 29th of 30) {p95:.2f} s, "
                  f"median {sorted(times)[len(times) // 2]:.2f} s; R130 {'holds' if p95 < 20 else 'FAILS'} "
                  f"(under 20 s); billed {billed / 2**20:.0f} MB, Athena {usd:.6f} USD, model calls {cad:.6f} CAD")
            return 0 if p95 < 20 else 1
        for q in args.question or QUESTIONS:
            print(json.dumps(line(one(q))))
        print(f"engine: {where}; model: {model_kind}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

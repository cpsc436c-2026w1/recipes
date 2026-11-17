"""Ask questions and print what the dashboard would show.

  uv run python -m main.run --target local --model stub         offline: stub model, traces in output/traces.jsonl
  uv run python -m main.run --target aws                        course LLM gateway, traces in DynamoDB
  uv run python -m main.run --target aws --question "How many orders are there?"
"""

from __future__ import annotations

import argparse
import json

from main.chat import answer
from main.config import OUTPUT_DIR, ROOT, Settings
from main.data import data_version, ensure_data
from main.engine import Engine
from main.model import GatewayModel, StubModel


def build(target: str, model_kind: str, settings: Settings):
    paths = ensure_data()
    engine = Engine(paths)
    if model_kind == "stub":
        model = StubModel.from_file(ROOT / "main" / "stub_replies.json")
    else:
        model = GatewayModel(settings.gateway_url, settings.token, settings.chat_model)
    if target == "aws":
        from main.infra import ensure_table
        from main.traces import DynamoTraces

        ensure_table(settings.traces_table, settings.region, key="request_id")
        traces = DynamoTraces(settings.traces_table, settings.region)
    else:
        from main.traces import LocalTraces

        traces = LocalTraces(OUTPUT_DIR / "traces.jsonl")
    return engine, model, traces, data_version(paths)


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--target", choices=["local", "aws"], default="local")
    p.add_argument("--model", choices=["stub", "gateway"], default=None,
                   help="default: stub for --target local, gateway for --target aws")
    p.add_argument("--user", default="c-0007", help="customer id of the person asking")
    p.add_argument("--question", action="append", help="ask this instead of main/questions.txt (repeatable)")
    args = p.parse_args(argv)
    settings = Settings()
    model_kind = args.model or ("stub" if args.target == "local" else "gateway")
    engine, model, traces, version = build(args.target, model_kind, settings)
    questions = args.question or (ROOT / "main" / "questions.txt").read_text().splitlines()
    total, asked, errors = 0.0, 0, 0
    for q in filter(None, questions):
        a = answer(q, user_id=args.user, model=model, engine=engine, data_version=version, traces=traces)
        total += a.cost_cad
        asked += 1
        errors += a.status == "error"
        line = {"question": q, "status": a.status, "reply": a.reply, "sql": a.sql,
                "rows": a.rows[:3], "model_calls": sum(s.kind == "model_call" for s in a.steps),
                "latency_ms": a.latency_ms, "request_id": a.request_id}
        if a.error:  # for example the gateway's 401, 403 or 429 with its reason (README, "Decisions": a failed call)
            line["error"] = a.error
        print(json.dumps(line))
    trace_file = OUTPUT_DIR / "traces.jsonl"
    if trace_file.is_relative_to(ROOT):
        trace_file = trace_file.relative_to(ROOT)
    where = settings.traces_table if args.target == "aws" else trace_file
    note = " (stub: tokens estimated from characters)" if model_kind == "stub" else ""
    print(f"model: {model_kind}; total {total:.6f} CAD{note}; traces in {where}")
    if errors:
        # The app went on to the next question each time; the run as a whole did not succeed.
        raise SystemExit(f"{errors} of {asked} questions ended with status error; each line's \"error\" says why")


if __name__ == "__main__":
    main()

"""Answer the main questions, write linked spans and metrics, and (on AWS) create the three alarms.

  uv run python ext-trace-alarms/trace_run.py --target local --model stub     spans in output/spans.jsonl
  uv run python ext-trace-alarms/trace_run.py --target aws [--email you@example.com]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alarms import ensure_alarms, put_request_metrics  # noqa: E402
from spans import DynamoSpans, LocalSpans, spans_of  # noqa: E402

from main.chat import answer  # noqa: E402
from main.config import OUTPUT_DIR, ROOT, Settings  # noqa: E402
from main.run import build  # noqa: E402


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--target", choices=["local", "aws"], default="local")
    p.add_argument("--model", choices=["stub", "gateway"], default=None)
    p.add_argument("--email", help="subscribe this address to the alarm topic (AWS only)")
    args = p.parse_args(argv)
    s = Settings()
    model_kind = args.model or ("stub" if args.target == "local" else "gateway")
    engine, model, traces, version = build(args.target, model_kind, s)
    if args.target == "aws":
        from main.infra import ensure_table

        ensure_table(s.spans_table, s.region, key="trace_id", sort_key="span_id")
        sink = DynamoSpans(s.spans_table, s.region)
        print("alarms:", ensure_alarms(s.alarm_prefix, s.metric_namespace, s.topic_name, s.region, args.email))
    else:
        sink = LocalSpans(OUTPUT_DIR / "spans.jsonl")
    for q in filter(None, (ROOT / "main" / "questions.txt").read_text().splitlines()):
        ans = answer(q, user_id="c-0007", model=model, engine=engine, data_version=version, traces=traces)
        spans = spans_of(ans)
        sink.put_all(spans)
        if args.target == "aws":
            put_request_metrics(ans.to_dict(), s.metric_namespace, s.region)
        print(f"{ans.request_id}  {len(spans) - 1} calls  {ans.latency_ms:8.1f} ms  {ans.status:9}  {q}")


if __name__ == "__main__":
    main()

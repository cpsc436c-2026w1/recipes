"""Mark an answer up or down, and list the answers marked down.

  uv run python ext-feedback/feedback_run.py mark <request_id> down      a local run's trace (output/traces.jsonl)
  uv run python ext-feedback/feedback_run.py mark <request_id> down --user c-0012     as another customer
  uv run python ext-feedback/feedback_run.py report
  uv run python ext-feedback/feedback_run.py --target aws mark <request_id> up      the table {PREFIX}-chat-traces
  uv run python ext-feedback/feedback_run.py --target aws report

The request id is in every line that main/run.py prints ("request_id"). In a dashboard, call `mark` from the
handler of a thumbs-up and a thumbs-down button under the reply (README, "Adapt it").
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # the recipe root, so `main` imports

from feedback import VERDICTS, DynamoFeedback, LocalFeedback, NoSuchTrace, report  # noqa: E402

from main.config import OUTPUT_DIR, Settings  # noqa: E402


def store(target: str):
    if target == "aws":
        s = Settings()
        return DynamoFeedback(s.traces_table, s.region)
    return LocalFeedback(OUTPUT_DIR / "traces.jsonl")


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--target", choices=["local", "aws"], default="local")
    sub = p.add_subparsers(dest="command", required=True)
    mark = sub.add_parser("mark", help="store a verdict on the trace item of one answer")
    mark.add_argument("request_id")
    mark.add_argument("verdict", help=f"one of: {', '.join(VERDICTS)}")
    mark.add_argument("--user", default="c-0007", help="customer id of the person who gives the verdict; only the "
                      "person who asked may mark an answer (default: the default asker of main/run.py)")
    sub.add_parser("report", help="count the verdicts and list the answers marked down")
    args = p.parse_args(argv)
    feedback = store(args.target)
    if args.command == "mark":
        try:
            item = feedback.mark(args.request_id, args.verdict, args.user)
        except (NoSuchTrace, ValueError) as e:  # nothing was written
            raise SystemExit(f"not stored: {e}") from None
        print(json.dumps({"request_id": item["request_id"], "question": item["question"],
                          "feedback": item["feedback"], "feedback_at": item["feedback_at"]}))
        return
    summary = report(feedback.items())
    print(f"{summary['answers']} answers: {summary['up']} marked up, {summary['down']} marked down, "
          f"{summary['not_marked']} not marked")
    for row in summary["marked_down"]:
        print(json.dumps(row, default=str))


if __name__ == "__main__":
    main()

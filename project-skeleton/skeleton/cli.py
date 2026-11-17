"""Command-line options every representative's run.py shares."""
import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from skeleton.events import generate, load_jsonl, from_streamsim, parse


def parser(description: str) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=description)
    p.add_argument("--target", choices=["local", "aws"], default="local",
                   help="local: offline stand-ins (default); aws: your sandbox in ca-central-1")
    p.add_argument("--events", help="your own updates: a JSONL file with event_id, entity_id, event_time, value")
    p.add_argument("--streamsim", help="a stream file written by 436c-stream-sim (streams/<name>.jsonl)")
    p.add_argument("--value-field", default="value", help="with --streamsim: the payload field to use as value")
    p.add_argument("--n", type=int, default=300, help="how many synthetic updates (when no file is given)")
    p.add_argument("--as-of", help="end of the hour to count, ISO time in UTC (default: now)")
    return p


def load(args) -> tuple[list[dict], datetime]:
    as_of = parse(args.as_of) if args.as_of else datetime.now(timezone.utc)
    if args.events:
        return load_jsonl(args.events), as_of
    if args.streamsim:
        return from_streamsim(args.streamsim, args.value_field), as_of
    return generate(args.n, now=as_of), as_of


def add_rep_paths() -> None:
    """Make the three rep-* folders importable (their names have hyphens, so they are not packages)."""
    root = Path(__file__).resolve().parent.parent
    for d in ("rep-lakehouse-first", "rep-operational-first", "rep-genai-first"):
        if str(root / d) not in sys.path:
            sys.path.insert(0, str(root / d))

"""Ask each question three times (twice as one user, once as another) with the answer cache in front of main.

  uv run python ext-answer-cache/cache_run.py --target local --model stub
  uv run python ext-answer-cache/cache_run.py --target aws
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # the recipe root, so `main` imports

from cache import DynamoCache, LocalCache, answer_version, cached_answer  # noqa: E402

from main.chat import answer  # noqa: E402
from main.config import ROOT, Settings  # noqa: E402
from main.run import build  # noqa: E402


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--target", choices=["local", "aws"], default="local")
    p.add_argument("--model", choices=["stub", "gateway"], default=None)
    args = p.parse_args(argv)
    s = Settings()
    model_kind = args.model or ("stub" if args.target == "local" else "gateway")
    engine, model, traces, version = build(args.target, model_kind, s)
    if args.target == "aws":
        from main.infra import ensure_table

        ensure_table(s.cache_table, s.region, key="cache_key")
        cache = DynamoCache(s.cache_table, s.region)
    else:
        cache = LocalCache()
    questions = [q for q in (ROOT / "main" / "questions.txt").read_text().splitlines() if q]
    hits = calls = 0
    for user in ("c-0007", "c-0007", "c-0012"):
        for q in questions:
            def ask(question, user=user):
                return answer(question, user_id=user, model=model, engine=engine, data_version=version,
                              traces=traces)
            d, hit = cached_answer(q, user_id=user, cache=cache, data_version=version, ask=ask,
                                   answer_version=answer_version(model.model))
            hits += hit
            calls += not hit
            print(json.dumps({"user": user, "question": q, "status": d["status"], "reply": d["reply"]}))
    print(f"{hits} cache hits, {calls} questions sent to the model")


if __name__ == "__main__":
    main()

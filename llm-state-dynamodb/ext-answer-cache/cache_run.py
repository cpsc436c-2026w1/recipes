"""Answer cache in front of retrieval: shared answers reused, personal answers kept per user, a version bump.

  uv run python ext-answer-cache/cache_run.py --target local     moto stands in for DynamoDB
  uv run python ext-answer-cache/cache_run.py --target aws       real table {PREFIX}-answer-cache

The retriever and the model are stubs here. In your app, `retrieve` is your search (../hybrid-retrieval-eval)
and `generate` is your model call through the course LLM gateway (../llm-gateway).
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from pathlib import Path

import boto3

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "main"))

from answer_cache import AnswerCache, answer_with_cache  # noqa: E402
from config import Settings  # noqa: E402
from run import offline_aws  # noqa: E402
from tables import ensure_tables  # noqa: E402

DOCS = [
    {"id": "policy-returns", "owner": "", "text": "Items can be returned within 30 days with a receipt."},
    {"id": "order-1042", "owner": "user-ana", "text": "Order 1042 shipped yesterday."},
    {"id": "order-2077", "owner": "user-ben", "text": "Order 2077 is waiting for stock."},
]


class Counter:
    retrievals = 0
    model_calls = 0


def stub_retrieve(question: str, user_id: str) -> list[dict]:
    """Public documents for everyone; private documents only for their owner (the retriever's own filter)."""
    Counter.retrievals += 1
    if "order" in question.lower():
        return [d for d in DOCS if d["owner"] == user_id]
    return [d for d in DOCS if d["owner"] == ""]


def stub_generate(question: str, docs: list[dict]) -> str:
    Counter.model_calls += 1
    return " ".join(d["text"] for d in docs) or "I could not find that."


def scenario(s: Settings, resource) -> list[str]:
    Counter.retrievals = Counter.model_calls = 0  # each run counts its own calls, also a second run in one process
    ensure_tables(s, ["cache"], resource.meta.client)
    cache = AnswerCache(s.cache_table, resource)
    asks = [("user-ana", "What is the return policy?", "idx-1"),
            ("user-ben", "what is the  return policy", "idx-1"),
            ("user-ana", "Where is my order?", "idx-1"),
            ("user-ben", "Where is my order?", "idx-1"),
            ("user-ana", "Where is my order?", "idx-1"),
            ("user-ben", "What is the return policy?", "idx-2")]
    lines = []
    for user, q, version in asks:
        ans, hit = answer_with_cache(q, user_id=user, index_version=version, cache=cache,
                                     retrieve=stub_retrieve, generate=stub_generate)
        lines.append(f"{user}  {version}  {'hit ' if hit else 'miss'}  {ans['scope']:<6}  {q!r} -> {ans['reply']}")
    lines.append(f"{len(asks)} questions, {cache.hits} hits, {cache.misses} misses, {Counter.retrievals} retrievals, "
                 f"{Counter.model_calls} model calls")
    return lines


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--target", choices=["local", "aws"], default="local")
    args = p.parse_args(argv)
    s = Settings()
    with offline_aws() if args.target == "local" else contextlib.nullcontext():
        print(f"target {args.target}  prefix {s.prefix}  region {s.region}")
        for line in scenario(s, boto3.resource("dynamodb", region_name=s.region)):
            print(line)


if __name__ == "__main__":
    main()

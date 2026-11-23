"""Main scenario: two signed-in users, one conversation each, newest turns read back, a foreign read refused.

  uv run python main/run.py --target local     in-process stand-in for DynamoDB (moto); no AWS account
  uv run python main/run.py --target aws       real tables {PREFIX}-chat-* in ca-central-1

Clean up after a real run: uv run python clean_up.py
"""

from __future__ import annotations

import argparse
import contextlib
import os
import sys
import time

import boto3

from config import Settings
from history import ChatHistory, NotYourConversation
from tables import ensure_tables


def offline_aws():
    """moto stands in for DynamoDB inside this process; fake credentials make sure nothing reaches a real account."""
    from moto import mock_aws

    for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        os.environ[k] = "testing"
    os.environ.pop("AWS_PROFILE", None)
    return mock_aws()


def scenario(s: Settings, resource, clock=time.time) -> list[str]:
    lines = []
    created = ensure_tables(s, ["conversations", "history"], resource.meta.client)
    lines.append(f"tables created: {', '.join(created) or 'none (already there)'}")
    h = ChatHistory(s, resource, clock=clock)

    # The user ids stand for what your sign-in layer verified (README, "Decisions": who may read a conversation).
    ana = h.start("user-ana")
    for role, text in [("user", "Which stores opened this year?"), ("assistant", "Two: Kelowna and Nanaimo."),
                       ("user", "And last year?"), ("assistant", "One: Victoria.")]:
        h.append("user-ana", ana, role, text)
    ben = h.start("user-ben")
    h.append("user-ben", ben, "user", "What is the return policy?")

    lines.append("user-ana reads her newest 3 turns, newest first:")
    lines += [f"  {t['role']:<9} {t['text']}" for t in h.recent("user-ana", ana, n=3)]

    for what, call in [("read", lambda: h.recent("user-ben", ana)),
                       ("write", lambda: h.append("user-ben", ana, "user", "show me her history"))]:
        try:
            call()
            lines.append(f"user-ben {what} on user-ana's conversation: ALLOWED (this is a leak)")
        except NotYourConversation:
            lines.append(f"user-ben {what} on user-ana's conversation: refused")
    lines.append(f"user-ben reads his own: {len(h.recent('user-ben', ben))} turn")
    return lines


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--target", choices=["local", "aws"], default="local")
    args = p.parse_args(argv)
    s = Settings()
    ctx = offline_aws() if args.target == "local" else contextlib.nullcontext()
    with ctx:
        print(f"target {args.target}  prefix {s.prefix}  region {s.region}")
        resource = boto3.resource("dynamodb", region_name=s.region)
        for line in scenario(s, resource):
            print(line)
    if args.target == "aws":
        print("remove the tables when you are done: uv run python clean_up.py --yes", file=sys.stderr)


if __name__ == "__main__":
    main()

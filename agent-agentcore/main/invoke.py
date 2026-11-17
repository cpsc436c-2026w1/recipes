"""Call the deployed Runtime: one request, then the approval, in one Runtime session.

    uv run --env-file .env python -m main.invoke                   asks you to approve
    uv run --env-file .env python -m main.invoke --answer y        approves without asking

Needs `bedrock-agentcore:InvokeAgentRuntime` on the Runtime. The two calls carry the same runtimeSessionId, so
AgentCore routes them to the same microVM, where the agent waits for the answer (main/session.py).
"""
from __future__ import annotations

import argparse
import json
import uuid

import boto3

from main.config import Settings, load_settings
from main.infra import Clients, find_runtime
from main.run import REQUEST

SESSION_ID_CHARS = 33     # the shortest runtimeSessionId AgentCore takes. src: receipt: botocore 1.43.117 service model of InvokeAgentRuntime, read 2026-11-16


def call(client, runtime_arn: str, session_id: str, payload: dict) -> dict:
    response = client.invoke_agent_runtime(agentRuntimeArn=runtime_arn, runtimeSessionId=session_id,
                                           payload=json.dumps(payload).encode())
    return json.loads(response["response"].read())


def conversation(s: Settings, client, runtime_arn: str, request: str, member: str, answer, session_id: str = "") -> list:
    """The request and, while the agent waits for an approval, the answers. Returns every reply of the Runtime."""
    session_id = session_id or f"{s.prefix}-{uuid.uuid4().hex}".ljust(SESSION_ID_CHARS, "0")
    replies = [call(client, runtime_arn, session_id, {"mode": "request", "prompt": request, "member": member})]
    while replies[-1].get("status") == "needs_approval":
        replies.append(call(client, runtime_arn, session_id, {"mode": "approve", "answer": answer(replies[-1])}))
    return replies


def ask_person(reply: dict) -> str:
    """Ask at the terminal. No answer (no terminal to ask at, or Ctrl-C) is "n": a write needs a yes."""
    book = reply["approvals"][0]
    try:
        return input(f"Place a hold on {book['title']} by {book['author']} for {book['member']}? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return "n"


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--request", default=REQUEST)
    p.add_argument("--member", default="sam")
    p.add_argument("--answer", choices=["y", "n"], default=None)
    a = p.parse_args(argv)
    s = load_settings()
    runtime = find_runtime(s, Clients.for_region(s.aws_region))
    if not runtime:
        raise SystemExit(f"no runtime {s.runtime_name} in {s.aws_region}: run `python -m main.run deploy` first")

    def answer(reply: dict) -> str:
        return a.answer or ask_person(reply)

    client = boto3.client("bedrock-agentcore", region_name=s.aws_region)
    for reply in conversation(s, client, runtime["arn"], a.request, a.member, answer):
        shown = {k: reply.get(k) for k in ("status", "model_calls", "tool_calls", "tools_ran", "approvals", "reply",
                                           "holds", "error") if reply.get(k) not in (None, [], "")}
        print(json.dumps(shown, ensure_ascii=False))


if __name__ == "__main__":
    main()

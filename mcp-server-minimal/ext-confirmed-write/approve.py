"""The person's side: show one change, ask yes or no, print the confirmation code.

Run (same APPROVAL_SECRET as the server; the command comes from the tool result):
    uv run python ext-confirmed-write/approve.py --request-id ... --order-id ... --amount ... --reason ... \
        --approver <your-name>

The script runs on the person's machine, outside the chat. The code it prints reaches the model only when the
person pastes it as a user message, so the decision stays with the person (ext README, "Decisions").
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from confirm import APPROVAL_TTL_S, Refund, make_code  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Approve one refund and print its confirmation code")
    p.add_argument("--request-id", required=True)
    p.add_argument("--order-id", required=True, type=int)
    p.add_argument("--amount", required=True, type=float)
    p.add_argument("--reason", required=True)
    p.add_argument("--approver", default=os.environ.get("APPROVER", ""), help="your name; stored with the refund")
    p.add_argument("--yes", action="store_true", help="skip the question (scripts and tests only)")
    a = p.parse_args(argv)
    secret = os.environ.get("APPROVAL_SECRET", "")
    if len(secret) < 16:
        print("set APPROVAL_SECRET to the server's approval secret first", file=sys.stderr)
        return 2
    if not a.approver:
        print("give --approver <your-name> (or set APPROVER)", file=sys.stderr)
        return 2
    refund = Refund(a.request_id, a.order_id, round(a.amount, 2), a.reason)
    print(f"Refund {refund.amount_cad:.2f} CAD on order {refund.order_id}")
    print(f"Reason: {refund.reason}")
    print(f"Request id: {refund.request_id}")
    if not a.yes and input("Approve this refund? [y/N] ").strip().lower() != "y":
        print("Not approved; no code.")
        return 1
    print(f"Confirmation code: {make_code(secret, a.approver, refund)}")
    print(f"It works for {APPROVAL_TTL_S // 60} minutes, for this change only.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

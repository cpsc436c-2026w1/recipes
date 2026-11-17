"""Walk the write flow against a running write_server.py, playing both the model and the person.

Run (write server running, same MCP_SERVER_TOKEN and APPROVAL_SECRET):
    uv run python ext-confirmed-write/walk.py --approver ana

Steps: a call without a code, a call with a made-up code, the person's approval (make_code, as approve.py does),
the call with the code, and the same call again. Each prints one JSON line.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from confirm import Refund, make_code  # noqa: E402
from main.client import connect        # noqa: E402
from main.config import Settings       # noqa: E402


async def walk(url: str, token: str, secret: str, approver: str, request_id: str = "2026-11-12-late-1042") -> list:
    args = {"request_id": request_id, "order_id": 1042, "amount_cad": 12.5, "reason": "arrived two days late"}
    steps = []
    async with connect(url, token) as client:
        async def call(label, **extra):
            r = await client.call_tool("record_refund", {**args, **extra})
            text = r.content[0].text
            steps.append({"step": label, "is_error": r.is_error, "result": text if r.is_error else json.loads(text)})

        await call("no code")
        await call("made-up code", confirmation=f"{approver}.9999999999.0123456789abcdef")
        code = make_code(secret, approver, Refund(args["request_id"], args["order_id"], args["amount_cad"],
                                                  args["reason"]))   # what the person gets from approve.py
        await call("code for a larger amount", confirmation=code, amount_cad=40.0)
        await call("with code", confirmation=code)
        await call("same call again", confirmation=code)
    return steps


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--approver", default="ana")
    p.add_argument("--request-id", default="2026-11-12-late-1042")
    a = p.parse_args(argv)
    s = Settings()
    for step in asyncio.run(walk(s.url, s.token, os.environ.get("APPROVAL_SECRET", ""), a.approver, a.request_id)):
        print(json.dumps(step))


if __name__ == "__main__":
    main()

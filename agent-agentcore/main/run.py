"""Command line of the recipe.

    uv run --env-file .env.example python -m main.run ask --model stub --yes     offline: no key, no account
    uv run --env-file .env python -m main.run ask --model gateway                one request on the course LLM gateway
    uv run --env-file .env python -m main.run deploy                             tables, Lambda, Gateway, Runtime
    uv run --env-file .env python -m main.run ask --tools gateway --model gateway    laptop agent, tools behind the Gateway

`ask` runs one request in this process and asks you to approve the write. One request makes at most
MAX_MODEL_CALLS model calls: the cap is a hook, whichever model answers.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys

from main import tracing
from main.agent import run_request
from main.config import Settings, load_settings

REQUEST = "Which is shorter, Dune or Emma? Place a hold on the shorter one for me."


def ask_person(approval: dict) -> bool:
    """Ask at the terminal. No answer (no terminal to ask at, or Ctrl-C) is a no: a write needs a yes."""
    try:
        answer = input(f"Place a hold on {approval['title']} by {approval['author']} for {approval['member']}? [y/N] ")
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer.strip().lower() in ("y", "yes")


def ask(s: Settings, a) -> dict:
    confirm = (lambda approval: True) if a.yes else (lambda approval: False) if a.no else ask_person
    if a.model == "stub":
        from main.model import StubModel

        model = StubModel(a.stub)
    else:
        from main.model import gateway_model

        model = gateway_model(s)          # stops here, before any model call, when the address or the token is empty
    client = None
    if a.tools == "gateway":
        from main.gateway_tools import GatewayLibrary, open_gateway
        from main.infra import Clients, find_gateway

        c = Clients.for_region(s.aws_region)
        gateway_id = find_gateway(s, c)
        if not gateway_id:
            raise SystemExit(f"no gateway {s.gateway_name} in {s.aws_region}: run `python -m main.run deploy` first")
        client = open_gateway(c.agentcore.get_gateway(gatewayIdentifier=gateway_id)["gatewayUrl"], s.aws_region)
        library = GatewayLibrary(client)
        tools = library.tools
    else:
        from main.store import MemoryStore
        from main.tools import LibraryTools

        library, tools = LibraryTools(MemoryStore()), None
    traced = tracing.configure(s)
    try:
        with tracing.request_span(traced, a.request_id or "", a.member):
            report = run_request(s, a.request, a.member, model, library, confirm, a.request_id, tools)
        report["holds"] = library.get_holds(a.member)
    finally:
        if client is not None:
            client.stop(None, None, None)
    return report


def main(argv: list[str] | None = None) -> None:
    logging.getLogger("strands").setLevel(logging.ERROR)
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    q = sub.add_parser("ask", help="one request, in this process")
    q.add_argument("--request", default=REQUEST)
    q.add_argument("--member", default="sam")
    q.add_argument("--model", choices=["stub", "gateway"], default="stub")
    q.add_argument("--stub", default="careful", help="the stub's behaviour (main/model.py)")
    q.add_argument("--tools", choices=["local", "gateway"], default="local")
    q.add_argument("--request-id", default=None, help="the same id twice shows the once-only write")
    q.add_argument("--yes", action="store_true", help="approve the write without asking")
    q.add_argument("--no", action="store_true", help="decline the write without asking")
    d = sub.add_parser("deploy", help="create or update everything in AWS")
    d.add_argument("--no-dependencies", action="store_true", help="zip the code without its packages (tests)")
    a = p.parse_args(argv)
    s = load_settings()                   # a missing setting ends the run here, before any AWS or model call
    if a.command == "deploy":
        from main.infra import Clients, deploy

        out = deploy(s, Clients.for_region(s.aws_region), with_dependencies=not a.no_dependencies)
        print(f"deployed for PREFIX={s.prefix} in {s.aws_region}; next: python -m main.invoke")
        return
    report = ask(s, a)
    if report["status"] == "error":
        print(f"error after {report['model_calls']} model calls: {report['error']}", file=sys.stderr)
        sys.exit(1)
    print(f"{report['status']}: {report['model_calls']} model calls, {report['tool_calls']} tool calls "
          f"(ran: {', '.join(report['tools_ran']) or 'none'}; refused: {len(report['tools_refused'])}); "
          f"approvals asked: {len(report['approvals_asked'])}")
    print("reply:", report.get("reply", ""))
    print("holds:", json.dumps(report["holds"], ensure_ascii=False))
    if report["status"] == "stopped":
        print("stopped by the app:", report["stopped"])
        sys.exit(2)


if __name__ == "__main__":
    main()

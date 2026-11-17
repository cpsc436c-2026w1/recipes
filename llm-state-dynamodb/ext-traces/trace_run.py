"""One request with three calls (model, tool, model), traced, then read back in order.

  uv run python ext-traces/trace_run.py --target local     moto stands in for DynamoDB
  uv run python ext-traces/trace_run.py --target aws       real table {PREFIX}-llm-traces

The model and the tool are stubs; token counts and prices are made-up example values.
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from pathlib import Path

import boto3

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "main"))

from config import Settings  # noqa: E402
from run import offline_aws  # noqa: E402
from tables import ensure_tables  # noqa: E402
from trace_store import Tracer, calls_of  # noqa: E402


def one_request(tracer: Tracer) -> None:
    with tracer.call("model", "plan") as c:
        c.tokens_in, c.tokens_out, c.cost_cad = 420, 30, 0.000024
    with tracer.call("tool", "search_orders"):
        pass
    try:
        with tracer.call("tool", "lookup_store"):
            raise TimeoutError("store service did not answer in 2 s")
    except TimeoutError:
        pass
    with tracer.call("model", "reply") as c:
        c.tokens_in, c.tokens_out, c.cost_cad = 610, 55, 0.000036


def scenario(s: Settings, resource) -> list[str]:
    ensure_tables(s, ["traces"], resource.meta.client)
    tracer = Tracer(s.traces_table, resource, user_id="user-ana", conversation_id="c-demo")
    one_request(tracer)
    lines = [f"request {tracer.request_id}"]
    for i in calls_of(s.traces_table, resource, tracer.request_id):
        lines.append(f"  {int(i['seq'])}  {i['kind']:<5}  {i['name']:<13}  {i['status']:<5}  "
                     f"in {int(i['tokens_in'])}  out {int(i['tokens_out'])}  {i['error']}".rstrip())
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

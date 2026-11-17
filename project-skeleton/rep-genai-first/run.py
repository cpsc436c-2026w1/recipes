"""Generative AI first, end to end. From the recipe folder:

    uv run python rep-genai-first/run.py                          # offline: stub model, local table (moto)
    uv run python rep-genai-first/run.py --llm gateway            # local table, a real model (needs the token)
    PREFIX=recipe-yourname uv run python rep-genai-first/run.py --target aws --llm gateway
                                                                  # after rep-operational-first/run.py --target aws

The same question asked of the lake, without a model: rep-genai-first/lake_example.py.
"""
import contextlib
import os

import boto3

from skeleton.cli import add_rep_paths, load, parser
from skeleton.config import REGION, account_id, names, prefix, session
from skeleton.events import iso
from skeleton.lake_local import LOCAL_ROOT
from skeleton.number import window
from skeleton.offline import fake_aws
from skeleton.page import write_page

add_rep_paths()
import genai  # noqa: E402
import operational  # noqa: E402
from stub import StubClient  # noqa: E402


def main():
    p = parser("A question -> course LLM gateway -> model reads DynamoDB through one read-only tool -> the number")
    p.add_argument("--question", default="How many updates arrived in the last hour?")
    have_token = all(genai.gateway_env())
    p.add_argument("--llm", choices=["stub", "gateway"], default="gateway" if have_token else "stub",
                   help="stub: a fixed reply, offline (default without a token); gateway: the course LLM gateway")
    p.add_argument("--model", default=os.environ.get("MODEL", genai.DEFAULT_MODEL))
    args = p.parse_args()
    events, as_of = load(args)
    start, end = window(as_of)
    client = StubClient(start, end) if args.llm == "stub" else genai.gateway_client()

    with (fake_aws() if args.target == "local" else contextlib.nullcontext()):
        if args.target == "local":
            table = f"{prefix()}-updates"
            built = operational.run_local(events, LOCAL_ROOT / "genai", as_of, table=table)
            ddb = boto3.client("dynamodb", region_name=REGION)
            print(f"1. table {table} built offline (moto): {built['sent']['stored']} updates stored")
            expected = built["api"]["updates_last_hour"]
        else:
            sess = session()
            table = names(account=account_id(sess))["table"]
            ddb = sess.client("dynamodb")
            print(f"1. table {table} in your sandbox (run rep-operational-first first)")
            expected = None

        print(f"2. user message: {args.question!r}; model: {'stub' if args.llm == 'stub' else args.model}; "
              f"window {iso(start)} to {iso(end)}")
        answer = genai.ask(args.question, client, args.model,
                           lambda s, e: genai.count_updates(ddb, table, s, e), start, end)
    for call, result in zip(answer["calls"], answer["results"]):
        print(f"3. tool call count_updates({call.get('start')}, {call.get('end')}): tool result {result['updates']}")
    if answer.get("error"):
        print(f"4. no reply after {answer['steps']} model calls ({answer['tokens_in']} tokens in, "
              f"{answer['tokens_out']} out): error: {answer['error']}")
        raise SystemExit(f"   not an answer: raise the cap for {args.model} in genai.py (MAX_TOKENS_BY_MODEL); "
                         "no page was written")
    print(f"4. reply after {answer['steps']} model calls ({answer['tokens_in']} tokens in, "
          f"{answer['tokens_out']} out): {answer['reply']}")
    print(f"5. reply states a number the tool returned: {'yes' if genai.grounded(answer) else 'NO'}"
          + (f"; the API's number is {expected}" if expected is not None else ""))
    if genai.grounded(answer):
        number = answer["results"][-1]["updates"]
        print(f"   page for users: {write_page(number, 'genai-first', 'the assistant, from the table')}")


if __name__ == "__main__":
    main()

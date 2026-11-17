"""Compare three policies on the labelled requests: all-cheap, all-strong, routed. Prints accuracy and cost.

  uv run python ext-model-routing/routing_eval.py                       stub replies (stub_replies.json)
  uv run python ext-model-routing/routing_eval.py --model gateway       the course LLM gateway (40 calls)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from router import run_policy  # noqa: E402

from main.config import OUTPUT_DIR, Settings, load_jsonl  # noqa: E402
from main.llm import StubChat  # noqa: E402
from main.run import make_search  # noqa: E402

POLICIES = ("all-cheap", "all-strong", "routed")


def stub_chats() -> tuple[StubChat, StubChat]:
    replies = json.loads((HERE / "stub_replies.json").read_text())
    from router import ROUTER_MODEL

    return StubChat({ROUTER_MODEL: replies["router"]}, default="hard"), StubChat(replies["answers"])


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--model", choices=["stub", "gateway"], default="stub")
    p.add_argument("--embedder", choices=["stub", "gateway"], default="stub")
    p.add_argument("--out", type=Path, default=OUTPUT_DIR, help="folder for the rows per request (default output/)")
    args = p.parse_args(argv)
    s = Settings()
    search = make_search("local", args.embedder, s)
    if args.model == "gateway":
        from main.llm import GatewayChat

        router_chat = answer_chat = GatewayChat(s.gateway_url, s.token)
    else:
        router_chat, answer_chat = stub_chats()
    requests = load_jsonl(HERE / "requests.jsonl")
    results = [run_policy(pol, requests, search, router_chat, answer_chat) for pol in POLICIES]
    print(f"{len(requests)} requests, {args.model} replies\n")
    print("| policy | accuracy | cost (CAD) | of which router | share sent to the stronger model |")
    print("|---|---:|---:|---:|---:|")
    for r in results:
        accuracy = "n/a" if r["accuracy"] is None else f"{r['accuracy']:.2f}"
        print(f"| {r['policy']} | {accuracy} | {r['cost_cad']:.6f} | {r['router_cost_cad']:.6f} | "
              f"{r['strong_share']:.2f} |")
    routed = results[-1]
    print(f"\nrouter label agrees with the hand label on {routed['router_agrees']:.2f} of the requests")
    # An empty reply is not scored (router.py). Say so under the table, so that an accuracy over fewer answers is
    # not read as one over all of them.
    for r in results:
        if r["errors"]:
            print(f"\n{r['policy']}: {len(r['errors'])} of {len(requests)} answers NOT SCORED, accuracy is over the "
                  f"other {r['scored']}")
            for e in r["errors"]:
                print(f"  {e['id']}  {e['model']}  {e['error']}")
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / f"routing-{args.model}.json").write_text(json.dumps(results, indent=1))


if __name__ == "__main__":
    main()

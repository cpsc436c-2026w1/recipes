"""Build both indexes, run the retrieval eval, then the answer eval, and print both tables.

  uv run python -m main.run                                       local indexes, stub embeddings, stub answers
  uv run python -m main.run --embedder gateway --answers gateway  real embeddings and answers, local indexes
  uv run python -m main.run --backend opensearch --embedder gateway --answers gateway
                                                                  OpenSearch Serverless in your sandbox
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from main.answer import FirstProductAnswerer, ModelAnswerer, evaluate_answers
from main.config import CHEAP_MODEL, EMBEDDING_MODEL, OUTPUT_DIR, ROOT, Settings, load_jsonl, load_products, load_queries
from main.embed import GatewayEmbedder, StubEmbedder
from main.llm import GatewayRefused
from main.evaluate import check_queries, evaluate, table
from main.search import LocalSearch


def make_search(backend: str, embedder_kind: str, s: Settings):
    embedder = GatewayEmbedder(s.gateway_url, s.token) if embedder_kind == "gateway" else StubEmbedder()
    products = load_products()
    if backend == "opensearch":
        from main.opensearch_store import OpenSearchSearch

        return OpenSearchSearch.create(products, embedder, s)
    return LocalSearch(products, embedder)


def _share(text: str) -> float:
    """A recall threshold: a number from 0 to 1."""
    value = float(text)
    if not 0 <= value <= 1:
        raise argparse.ArgumentTypeError(f"{text} is not between 0 and 1: recall is a share of the relevant products")
    return value


def lineage(args, s: Settings) -> dict:
    """What produced the numbers of a rows file: the data (by content, not only by name), the models and the
    parameters. Two rows files can then be compared knowing what changed between them (Data Analytics Lens, best
    practice 7.3: trace data lineage)."""
    import hashlib
    from datetime import datetime, timezone

    from main import bm25, fusion, search as search_module
    from main.config import DATA_DIR

    files = [DATA_DIR / "products.jsonl", args.queries or DATA_DIR / "queries.jsonl"]
    return {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "data": {Path(f).name: hashlib.sha256(Path(f).read_bytes()).hexdigest() for f in files},
        "backend": args.backend,
        "embedder": EMBEDDING_MODEL if args.embedder == "gateway" else "stub",
        "answers": CHEAP_MODEL if args.answers == "gateway" else "stub",
        "k": args.k, "rrf_k": fusion.RRF_K, "pool": search_module.POOL, "bm25_k1": bm25.K1, "bm25_b": bm25.B,
        "collection": s.collection if args.backend == "opensearch" else None,
    }


def gateway_spend(embedder, answerer) -> str:
    """One line with what this run sent to the gateway and what it cost at the gateway's prices; empty offline.
    GENOPS02-BP02 (monitor the model's metrics: tokens and cost)."""
    from main.llm import cost_cad

    parts, total = [], 0.0
    if hasattr(embedder, "requests"):         # the gateway embedder counts its requests; the stub does not
        parts.append(f"{embedder.requests} embedding requests, {embedder.tokens_in} tokens")
        total += cost_cad(EMBEDDING_MODEL, embedder.tokens_in, 0)
    if hasattr(answerer, "calls"):
        parts.append(f"{answerer.calls} chat calls, {answerer.tokens_in} input and {answerer.tokens_out} output tokens")
        total += answerer.spent_cad
    return f"gateway: {'; '.join(parts)}; {total:.6f} CAD at the gateway's prices" if parts else ""


def main(argv=None) -> None:
    """Runs the evals; a refused or failed gateway call ends the run with one line and exit code 1."""
    try:
        _main(argv)
    except GatewayRefused as err:
        raise SystemExit(f"stopped: the gateway {err}") from err


def _main(argv=None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--backend", choices=["local", "opensearch"], default="local")
    p.add_argument("--embedder", choices=["stub", "gateway"], default="stub")
    p.add_argument("--answers", choices=["stub", "gateway"], default="stub")
    p.add_argument("--k", type=int, default=5)
    p.add_argument("--queries", type=Path, default=None, help="labelled queries (default data/queries.jsonl)")
    p.add_argument("--out", type=Path, default=OUTPUT_DIR, help="folder for the rows per query (default output/)")
    p.add_argument("--min-recall", type=_share, default=None,
                   help="exit 1 when hybrid recall@k is below this (a gate for CI or a scheduled eval)")
    args = p.parse_args(argv)
    s = Settings()
    queries = load_jsonl(args.queries) if args.queries else load_queries()
    problems = check_queries(queries, {p["id"] for p in load_products()})
    if problems:   # before any model call or AWS call
        raise SystemExit("the labelled queries cannot be scored:\n  " + "\n  ".join(problems))
    search = make_search(args.backend, args.embedder, s)
    # Decision (gateway refusals): all queries in one embedding request; GatewayEmbedder keeps the vectors, so the
    # searches below do not call the gateway once per query.
    search.embedder.embed([q["query"] for q in queries])
    result = evaluate(search, queries, args.k)
    print(f"Retrieval ({len(queries)} labelled queries, {args.backend} indexes, {args.embedder} embeddings)\n")
    print(table(result))
    if args.answers == "gateway":
        from main.llm import GatewayChat

        answerer = ModelAnswerer(GatewayChat(s.gateway_url, s.token), CHEAP_MODEL)
    else:
        answerer = FirstProductAnswerer()
    ans = evaluate_answers(search, answerer, queries, args.k)
    print(f"\nAnswers ({args.answers} answerer, from the hybrid top {args.k})\n")
    print("| correct | grounded | retrieval hit, answer wrong | retrieval missed |")
    print("|---:|---:|---:|---:|")
    print(f"| {ans['answer_correct']:.2f} | {ans['answer_grounded']:.2f} | {ans['hit_but_wrong']} | "
          f"{ans['missed_retrieval']} |")
    spend = gateway_spend(search.embedder, answerer)
    if spend:
        print(f"\n{spend}")
    args.out.mkdir(parents=True, exist_ok=True)
    out = args.out / f"eval-{args.backend}-{args.embedder}-{args.answers}.json"
    out.write_text(json.dumps({"run": lineage(args, s), "retrieval": result, "answers": ans}, indent=1))
    shown = out.relative_to(ROOT) if out.resolve().is_relative_to(ROOT) else out
    print(f"\nrows per query: {shown}")
    hybrid = result["summary"]["hybrid"]["recall"]
    # Compared as printed, to two decimals: a table that shows 0.67 passes --min-recall 0.67.
    if args.min_recall is not None and round(hybrid, 2) < args.min_recall:
        raise SystemExit(f"hybrid recall@{args.k} {hybrid:.2f} is below --min-recall {args.min_recall:.2f}")


if __name__ == "__main__":
    main()

"""Filtered queries, inside the search and after it, with recall@5 and facet counts.

  uv run python ext-filters/filters_run.py                         local indexes, stub embeddings
  uv run python ext-filters/filters_run.py --backend opensearch --embedder gateway
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from filters import facets, filtered_hybrid, post_filter  # noqa: E402

from main.config import Settings, load_jsonl  # noqa: E402
from main.evaluate import recall_at  # noqa: E402
from main.run import make_search  # noqa: E402


def compare(search, queries: list[dict], k: int = 5) -> list[dict]:
    rows = []
    for q in queries:
        rel = set(q["relevant"])
        inside = [i for i, _ in filtered_hybrid(search, q["query"], q["filter"], k)]
        after = [i for i, _ in post_filter(search, q["query"], q["filter"], k)]
        rows.append({"id": q["id"], "query": q["query"], "filter": q["filter"],
                     "inside": inside, "after": after,
                     "recall_inside": recall_at(inside, rel, k), "recall_after": recall_at(after, rel, k)})
    return rows


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--backend", choices=["local", "opensearch"], default="local")
    p.add_argument("--embedder", choices=["stub", "gateway"], default="stub")
    args = p.parse_args(argv)
    search = make_search(args.backend, args.embedder, Settings())
    queries = load_jsonl(HERE / "filter_queries.jsonl")
    search.embedder.embed([q["query"] for q in queries])  # one embedding request for all queries (main/embed.py)
    rows = compare(search, queries)
    print("| query | filter | results inside | results after | recall@5 inside | recall@5 after |")
    print("|---|---|---:|---:|---:|---:|")
    for r in rows:
        print(f"| {r['query']} | {r['filter']} | {len(r['inside'])} | {len(r['after'])} | "
              f"{r['recall_inside']:.2f} | {r['recall_after']:.2f} |")
    n = len(rows)
    print(f"\nmean recall@5: inside {sum(r['recall_inside'] for r in rows) / n:.2f}, "
          f"after {sum(r['recall_after'] for r in rows) / n:.2f}")
    # Facets come from the products that match the words (keyword matches), before the user picks a filter.
    # On the laptop that is the BM25 match set; on OpenSearch, filters.opensearch_facets_body.
    matched = {i for i, _ in search.keyword("wireless", 50)}
    print("\nfacets for the keyword matches of 'wireless':", facets(search.products, matched))


if __name__ == "__main__":
    main()

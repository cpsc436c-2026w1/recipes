"""Retrieval eval: recall@k and reciprocal rank per method, over the labelled queries in data/queries.jsonl.

recall@k     the share of a query's relevant products that are in the top k (1.0 when all of them are)
reciprocal   1 / the rank of the first relevant product (0 when none is in the top 10); its mean is MRR

This measures retrieval only. Whether the model then writes a good answer is measured separately (main/answer.py):
a wrong answer after a good retrieval is a prompt or model problem; a wrong answer after a missed retrieval is a
retrieval problem, and only the separate numbers tell you which one to fix (README, "Decisions").
"""

from __future__ import annotations

METHODS = ("keyword", "embedding", "hybrid")


def check_queries(queries: list[dict], product_ids: set[str]) -> list[str]:
    """What is wrong with the labelled queries, one line per problem; empty when they can be scored. A relevant id
    that is not in the catalogue can never be found and would lower every method's score without a sign
    (Data Analytics Lens, best practice 1.1: validate the data before it is used)."""
    problems, seen = [], set()
    for n, q in enumerate(queries, 1):
        where = f"line {n} ({q.get('id', 'no id')})"
        for field in ("id", "query", "kind"):
            if not str(q.get(field, "")).strip():
                problems.append(f"{where}: no {field}")
        if q.get("id") in seen:
            problems.append(f"{where}: the id is used twice")
        seen.add(q.get("id"))
        relevant = q.get("relevant") or []
        if not relevant:
            problems.append(f"{where}: no relevant product ids")
        for pid in relevant:
            if pid not in product_ids:
                problems.append(f"{where}: relevant id {pid} is not in the catalogue")
    return problems


def recall_at(ranked: list[str], relevant: set[str], k: int) -> float:
    return len(set(ranked[:k]) & relevant) / len(relevant)


def reciprocal_rank(ranked: list[str], relevant: set[str]) -> float:
    for rank, doc in enumerate(ranked[:10], start=1):
        if doc in relevant:
            return 1.0 / rank
    return 0.0


def evaluate(search, queries: list[dict], k: int = 5) -> dict:
    rows = []
    for q in queries:
        relevant = set(q["relevant"])
        for m in METHODS:
            ranked = [d for d, _ in getattr(search, m)(q["query"], 10)]
            rows.append({"id": q["id"], "kind": q["kind"], "method": m, "top": ranked[:k],
                         "recall": recall_at(ranked, relevant, k), "rr": reciprocal_rank(ranked, relevant)})
    summary = {}
    for m in METHODS:
        mine = [r for r in rows if r["method"] == m]
        summary[m] = {"recall": sum(r["recall"] for r in mine) / len(mine), "mrr": sum(r["rr"] for r in mine) / len(mine)}
        for kind in sorted({r["kind"] for r in mine}):
            part = [r for r in mine if r["kind"] == kind]
            summary[m][kind] = sum(r["recall"] for r in part) / len(part)
    return {"k": k, "summary": summary, "rows": rows}


def table(result: dict) -> str:
    kinds = sorted({r["kind"] for r in result["rows"]})
    head = f"| method | recall@{result['k']} | MRR | " + " | ".join(kinds) + " |"
    lines = [head, "|---|---:|---:|" + "---:|" * len(kinds)]
    for m, s in result["summary"].items():
        lines.append(f"| {m} | {s['recall']:.2f} | {s['mrr']:.2f} | " + " | ".join(f"{s[k]:.2f}" for k in kinds) + " |")
    return "\n".join(lines)

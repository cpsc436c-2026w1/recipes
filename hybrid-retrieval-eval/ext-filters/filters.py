"""Label and range filters inside the search, and facet counts (ext-filters/README.md).

A filter is a dict: {"category": ["audio"], "in_stock": True, "price_cad": {"lte": 100}}. The same dict becomes
- a set of allowed ids for the local indexes (allowed_ids), applied before ranking, and
- OpenSearch filter clauses (opensearch_filters), used inside the `match` and inside the `knn` query.

Inside, not after: a search that first takes the top k and then drops what fails the filter can return fewer than k
results, or none, although enough matching products exist (post_filter shows it; the test measures it).
"""

from __future__ import annotations

from collections import Counter

PRICE_BANDS = [("under 50", None, 50), ("50 to 200", 50, 200), ("200 and up", 200, None)]  # CAD, ours


def matches(p: dict, f: dict) -> bool:
    for field, cond in f.items():
        v = p[field]
        if isinstance(cond, list):
            if v not in cond:
                return False
        elif isinstance(cond, dict):
            if "gte" in cond and not v >= cond["gte"]:
                return False
            if "lte" in cond and not v <= cond["lte"]:
                return False
            if "lt" in cond and not v < cond["lt"]:
                return False
        elif v != cond:
            return False
    return True


def allowed_ids(products: dict[str, dict], f: dict) -> set[str]:
    return {i for i, p in products.items() if matches(p, f)}


def opensearch_filters(f: dict) -> list[dict]:
    out = []
    for field, cond in f.items():
        if isinstance(cond, list):
            out.append({"terms": {field: cond}})
        elif isinstance(cond, dict):
            out.append({"range": {field: cond}})
        else:
            out.append({"term": {field: cond}})
    return out


def filtered_hybrid(search, query: str, f: dict, k: int = 5):
    """Filter inside the search; works on LocalSearch (allowed ids) and on OpenSearchSearch (filter clauses)."""
    if hasattr(search, "client"):
        return search.hybrid(query, k, filters=opensearch_filters(f))
    return search.hybrid(query, k, allowed=allowed_ids(search.products, f))


def post_filter(search, query: str, f: dict, k: int = 5):
    """The approach to avoid: top k first, filter second."""
    return [(i, s) for i, s in search.hybrid(query, k) if matches(search.products[i], f)]


def facets(products: dict[str, dict], ids: set[str]) -> dict:
    """Counts per category, brand and price band over the products that pass the filter (and match the query,
    when `ids` is the match set). Shown next to the results so a user sees how to narrow the search."""
    chosen = [products[i] for i in sorted(ids)]   # sorted: the same order of ties on every run
    bands = Counter()
    for p in chosen:
        for name, lo, hi in PRICE_BANDS:
            if (lo is None or p["price_cad"] >= lo) and (hi is None or p["price_cad"] < hi):
                bands[name] += 1
    return {"category": dict(Counter(p["category"] for p in chosen).most_common()),
            "brand": dict(Counter(p["brand"] for p in chosen).most_common()),
            "price": {name: bands[name] for name, _, _ in PRICE_BANDS}}


def opensearch_facets_body(query: str, f: dict) -> dict:
    """The same counts from OpenSearch: size 0, the filtered match query, three aggregations."""
    q = {"multi_match": {"query": query, "fields": ["name^2", "description", "category", "brand"]}}
    return {
        "size": 0,
        "query": {"bool": {"must": [q], "filter": opensearch_filters(f)}},
        "aggs": {
            "category": {"terms": {"field": "category"}},
            "brand": {"terms": {"field": "brand"}},
            "price": {"range": {"field": "price_cad", "ranges": [
                {"key": n, **({"from": lo} if lo is not None else {}), **({"to": hi} if hi is not None else {})}
                for n, lo, hi in PRICE_BANDS]}},
        },
    }

from pathlib import Path

from main.config import load_jsonl

ROOT = Path(__file__).resolve().parent.parent


# ---- filters ----

def test_filter_inside_never_returns_fewer_than_after(search):
    from filters_run import compare

    rows = compare(search, load_jsonl(ROOT / "ext-filters" / "filter_queries.jsonl"))
    for r in rows:
        assert len(r["inside"]) >= len(r["after"]), r["id"]
        assert r["recall_inside"] >= r["recall_after"], r["id"]
    assert any(r["recall_inside"] > r["recall_after"] for r in rows)  # "two" + outdoor: the tent is found only inside
    for r in rows:
        assert all(search.products[i]["category"] in r["filter"].get("category", [search.products[i]["category"]])
                   for i in r["inside"])


def test_filter_clauses_and_facets(search):
    from filters import allowed_ids, facets, opensearch_facets_body, opensearch_filters

    f = {"category": ["audio"], "in_stock": True, "price_cad": {"lte": 100}}
    assert opensearch_filters(f) == [{"terms": {"category": ["audio"]}}, {"term": {"in_stock": True}},
                                     {"range": {"price_cad": {"lte": 100}}}]
    ids = allowed_ids(search.products, f)
    assert ids == {"P04", "P07", "P08"}
    counts = facets(search.products, ids)
    assert counts["category"] == {"audio": 3} and sum(counts["price"].values()) == 3
    body = opensearch_facets_body("speaker", f)
    assert body["size"] == 0 and set(body["aggs"]) == {"category", "brand", "price"}


# ---- model routing ----

def test_routing_policies(search):
    from router import label_of
    from routing_eval import stub_chats

    from router import run_policy

    assert label_of(" Simple. ") == "simple" and label_of("I think it is hard") == "hard"
    requests = load_jsonl(ROOT / "ext-model-routing" / "requests.jsonl")
    results = {}
    for pol in ("all-cheap", "all-strong", "routed"):
        router_chat, answer_chat = stub_chats()
        results[pol] = run_policy(pol, requests, search, router_chat, answer_chat)
    cheap, strong, routed = results["all-cheap"], results["all-strong"], results["routed"]
    assert cheap["cost_cad"] < routed["cost_cad"] < strong["cost_cad"]
    assert cheap["accuracy"] < routed["accuracy"] <= strong["accuracy"]
    assert routed["router_cost_cad"] > 0 and routed["router_agrees"] == 0.8
    assert 0 < routed["strong_share"] < 1

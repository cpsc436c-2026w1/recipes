import json
import math

import httpx
import numpy as np

from main.bm25 import BM25Index
from main.embed import GatewayEmbedder, StubEmbedder
from main.evaluate import evaluate, recall_at, reciprocal_rank
from main.fusion import rrf
from main.text import tokens


def test_tokens_keep_codes_whole():
    assert tokens("Lumo HX-200 USB-C chargers") == ["lumo", "hx-200", "usb-c", "charger"]


def test_bm25_by_hand():
    idx = BM25Index({"a": "red apple", "b": "green apple pie", "c": "pear"})
    n, df_red = 3, 1
    idf = math.log(1 + (n - df_red + 0.5) / (df_red + 0.5))
    avgdl = (2 + 3 + 1) / 3
    expected = idf * 1 * 2.2 / (1 + 1.2 * (1 - 0.75 + 0.75 * 2 / avgdl))
    (doc, score), = idx.search("red")
    assert doc == "a" and abs(score - expected) < 1e-12
    assert idx.search("banana") == []  # a word in no document: no match at all


def test_rrf_uses_ranks_not_scores():
    keyword = [("a", 25.0), ("b", 3.0)]
    vector = [("b", 0.91), ("c", 0.90)]
    fused = dict(rrf([keyword, vector], k=60))
    assert fused["b"] == 1 / 62 + 1 / 61  # rank 2 in one list, rank 1 in the other
    assert fused["a"] == 1 / 61 and fused["c"] == 1 / 62
    assert list(dict(rrf([keyword, vector]))) == ["b", "a", "c"]
    # scaling one list's scores changes nothing
    assert rrf([[("a", 2500.0), ("b", 300.0)], vector]) == rrf([keyword, vector])


def test_metrics():
    assert recall_at(["x", "a", "y"], {"a", "b"}, 2) == 0.5
    assert reciprocal_rank(["x", "a"], {"a"}) == 0.5
    assert reciprocal_rank(["x"], {"a"}) == 0.0


def test_stub_embedder_is_deterministic_and_unit_length():
    e = StubEmbedder()
    v1, v2 = e.embed(["comfy couch"]), e.embed(["comfy couch"])
    assert np.array_equal(v1, v2) and abs(np.linalg.norm(v1[0]) - 1) < 1e-6


def test_eval_shape_and_relations(search, queries):
    result = evaluate(search, queries, k=5)
    s = result["summary"]
    assert len(result["rows"]) == 3 * len(queries)
    # the relations the README states: keyword misses typos; hybrid is at least as good as keyword
    assert s["keyword"]["typo"] < 1.0
    assert s["hybrid"]["recall"] >= s["keyword"]["recall"]
    assert s["hybrid"]["mrr"] >= s["embedding"]["mrr"]
    blutooth = next(r for r in result["rows"] if r["id"] == "q10" and r["method"] == "keyword")
    assert blutooth["top"] == []


def test_answer_eval_is_separate(search, queries):
    from main.answer import FirstProductAnswerer, evaluate_answers, judge

    assert judge("Try [P01] or [P99].", ["P01"], {"P01"}) == {"cited": ["P01", "P99"], "correct": True,
                                                             "grounded": False}
    a = evaluate_answers(search, FirstProductAnswerer(), queries)
    assert a["answer_grounded"] == 1.0
    assert a["hit_but_wrong"] + a["missed_retrieval"] == sum(not r["correct"] for r in a["rows"])


def test_gateway_embedder_request_shape():
    seen = {}

    def handler(request):
        body = json.loads(request.content)
        seen.update(url=str(request.url), body=body, auth=request.headers["authorization"])
        data = [{"index": i, "embedding": [float(i + 1), 0.0, 0.0]} for i in range(len(body["input"]))][::-1]
        return httpx.Response(200, json={"data": data, "usage": {"prompt_tokens": 7}})

    http = httpx.Client(base_url="https://gw.example/v1", transport=httpx.MockTransport(handler),
                        headers={"Authorization": "Bearer tok"})
    e = GatewayEmbedder("https://gw.example/v1", "tok", http=http)
    v = e.embed(["a", "b"])
    assert seen["url"] == "https://gw.example/v1/embeddings" and seen["body"]["model"] == "qwen3-embedding-0.6b"
    assert seen["auth"] == "Bearer tok" and v.shape == (2, 3) and e.tokens_in == 7
    assert np.allclose(np.linalg.norm(v, axis=1), 1)

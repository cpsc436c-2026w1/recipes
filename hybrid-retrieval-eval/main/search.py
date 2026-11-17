"""Hybrid search on the laptop: BM25 and embedding search over the same products, fused by RRF.

The OpenSearch version (main/opensearch_store.py) has the same three methods, so evaluate.py runs on either.
"""

from __future__ import annotations

from main.bm25 import BM25Index
from main.config import doc_text
from main.fusion import rrf
from main.vector import VectorIndex

POOL = 20  # each list contributes its top 20 to the fusion (ours: deeper than the k of recall@k we report)


class LocalSearch:
    def __init__(self, products: list[dict], embedder):
        self.products = {p["id"]: p for p in products}
        texts = {p["id"]: doc_text(p) for p in products}
        self.bm25 = BM25Index(texts)
        self.embedder = embedder
        ids = list(texts)
        self.vectors = VectorIndex(ids, embedder.embed([texts[i] for i in ids]))

    def keyword(self, query: str, k: int = 10, allowed: set[str] | None = None):
        return self.bm25.search(query, k, allowed)

    def embedding(self, query: str, k: int = 10, allowed: set[str] | None = None):
        return self.vectors.search(self.embedder.embed([query])[0], k, allowed)

    def hybrid(self, query: str, k: int = 10, allowed: set[str] | None = None):
        return rrf([self.keyword(query, POOL, allowed), self.embedding(query, POOL, allowed)], top=k)
